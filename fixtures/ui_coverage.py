"""UI Coverage for a CFME/MIQ Appliance

Usage
-----

``py.test --ui-coverage``

General Notes
-------------
simplecov can merge test results, but doesn't appear to like working in a
multi-process environment. Specifically, it clobbers its own results when running
simultaneously in multiple processes. To solve this, each process records its
output to its own directory (configured in coverage_hook).  You end up with a
directory structure like this:

.. code-block:: text

    coverage-\
             |-$ip1-\
             .      |-$pid1-\
             .      .       |-.resultset.json (coverage statistics)
             .      .       |-.last_run.json  (overall coverage percentage)
             .      .
             .      |-$pidN
             .
             |-$ipN

Note the .resultset.json format is documented in the ruby Coverage libraries docs:

    http://ruby-doc.org/stdlib-2.1.0/libdoc/coverage/rdoc/Coverage.html

All of the individual process' results are then manually merged (coverage_merger) into one
big json result, and handed back to simplecov which generates the compiled html
(for humans) report.

Workflow Overview
-----------------

Pre-testing (``pytest_configure`` hook):

1. Add ``Gemfile.dev.rb`` to the rails root, then run bundler to install simplecov
   and its dependencies.
2. Patch application with manageiq-17302 patch so that coverage_hook will be loaded
   by the application.  Eventually this will be in CFME and we won't have to do this.
3. Install coverage hook (copy ``coverage_hook`` to config/).
4. Restart EVM to start running coverage on the appliance processes.

Post-testing (``pytest_unconfigure`` hook):

1. Stop EVM, but nicely this time so the coverage atexit hooks run:
   ``systemctl stop evmserverd``
2. Pull the coverage dir back for parsing and archiving

Post-testing (e.g. ci environment): *** This is changing ***

1. Use the generated rcov report with the ruby stats plugin to get a coverage graph
2. Zip up and archive the entire coverage dir for review
"""
import subprocess

import pytest
from py.error import ENOENT
from py.path import local

from fixtures.pytest_store import store
from cfme.exceptions import ApplianceVersionException
from cfme.utils import conf, version
from cfme.utils.conf import cfme_data
from cfme.utils.log import create_sublogger
from cfme.utils.path import conf_path, log_path, scripts_data_path
from cfme.utils.quote import quote

# paths to all of the coverage-related files

# on the appliance
#: Corresponds to Rails.root in the rails env
rails_root = local('/var/www/miq/vmdb')
#: coverage root, should match what's in the coverage hook and merger scripts
appliance_coverage_root = rails_root.join('coverage')

# local
coverage_data = scripts_data_path.join('coverage')
gemfile = coverage_data.join('coverage_gem.rb')
bundler_d = rails_root.join('bundler.d')
coverage_hook_file_name = 'coverage_hook.rb'
coverage_hook = coverage_data.join(coverage_hook_file_name)
coverage_merger = coverage_data.join('coverage_merger.rb')
coverage_output_dir = log_path.join('coverage')
coverage_results_archive = coverage_output_dir.join('coverage-results.tgz')
coverage_appliance_conf = conf_path.join('.ui-coverage')

# This is set in sessionfinish, and should be reliably readable
# in post-yield sessionfinish hook wrappers and all hooks thereafter
ui_coverage_percent = None


def clean_coverage_dir():
    try:
        coverage_output_dir.remove(ignore_errors=True)
    except ENOENT:
        pass
    coverage_output_dir.ensure(dir=True)


def manager():
    return store.current_appliance.coverage


# you probably don't want to instantiate this manually
# instead, use the "manager" function above
class CoverageManager(object):
    def __init__(self, ipappliance):
        self.ipapp = ipappliance
        if store.slave_manager:
            sublogger_name = '{} coverage'.format(store.slave_manager.slaveid)
        else:
            sublogger_name = 'coverage'
        self.log = create_sublogger(sublogger_name)
        # We don't know exactly when the forking architecture change
        # occurred in CFME, but this code does not accurately gather
        # coverage statistics from anything below CFME 5.8.   So if the
        # version is below 5.8, we set our functions to noops.
        if self.ipapp.version < '5.8':
            raise ApplianceVersionException(
                msg='Coverage statistics collection is only supported in appliances >= 5.8',
                version=self.ipapp.version)

    @property
    def collection_appliance(self):
        # if parallelized, this is decided in sessionstart and written to the conf
        if store.parallelizer_role == 'slave':
            from cfme.utils.appliance import IPAppliance
            return IPAppliance.from_url(conf['.ui-coverage']['collection_appliance'])
        else:
            # otherwise, coverage only happens on one appliance
            return store.current_appliance

    def print_message(self, message):
        self.log.info(message)
        message = 'coverage: {}'.format(message)
        if store.slave_manager:
            store.slave_manager.message(message)
        elif store.parallel_session:
            store.parallel_session.print_message(message)
        else:
            store.terminalreporter.write_sep('-', message)

    def install(self):
        self.print_message('installing')
        self._install_simplecov()
        self._install_coverage_hook()
        self.ipapp.restart_evm_service()
        self.ipapp.wait_for_web_ui()

    def collect(self):
        self.print_message('collecting reports')
        self._collect_reports()
        self.ipapp.restart_evm_service()

    def merge(self):
        self.print_message('merging reports')
        try:
            self._retrieve_coverage_reports()
            # If the appliance runs out of memory, these can take *days* to complete,
            # so for now we'll just collect the raw coverage data and figure the merging
            # out later
            # Edit, 10-Feb-2016:
            # Currently, the reports are merged using the {stream}-reports job
            # which utilizes the 'jjb/scripts/stream_reporter.sh' script instead
            # self._merge_coverage_reports()
            # self._retrieve_merged_reports()
        except Exception as exc:
            self.log.error('Error merging coverage reports')
            self.log.exception(exc)
            self.print_message('merging reports failed, error has been logged')

    def _install_simplecov(self):
        self.log.info('Installing coverage gem on appliance')
        self.ipapp.ssh_client.put_file(gemfile.strpath, bundler_d.strpath)

        # gem install for more recent downstream builds
        def _gem_install():
            self.ipapp.ssh_client.run_command(
                'gem install --install-dir /opt/rh/cfme-gemset/ -v0.9.2 simplecov')

        # bundle install for old downstream and upstream builds
        def _bundle_install():
            self.ipapp.ssh_client.run_command('yum -y install git')
            self.ipapp.ssh_client.run_command('cd {}; bundle'.format(rails_root))

        version.pick({
            version.LOWEST: _gem_install,
            version.LATEST: _bundle_install,
        })()

    def _install_coverage_hook(self):
        # Clean appliance coverage dir
        self.ipapp.ssh_client.run_command('rm -rf {}'.format(appliance_coverage_root.strpath))
        # Put the coverage hook in the miq config path
        self.ipapp.ssh_client.put_file(
            coverage_hook.strpath,
            rails_root.join('config', coverage_hook_file_name).strpath)
        # XXX: Once the manageiq PR 17302 makes it into the 5.9 and 5.8 stream we
        #      can remove all the code in this function after this.   This is only
        #      a temporary fix so we can start acquiring code coverage statistics.
        #
        # See if we need to install the patch.   If not just return.
        # The patch will create the file lib/code_coverage.rb under the rails root.
        # so if that is there we assume the patch is already installed.
        result = self.ipapp.ssh_client.run_command('cd {}; [ -e lib/code_coverage.rb ]'.format(
            rails_root))
        if result.success:
            return True
        # place patch on the system
        self.log.info('Patching system with manageiq patch #17302')
        coverage_hook_patch_name = 'manageiq-17302.patch'
        local_coverage_hook_patch = coverage_data.join(coverage_hook_patch_name)
        remote_coverage_hook_patch = rails_root.join(coverage_hook_patch_name)
        self.ipapp.ssh_client.put_file(
            local_coverage_hook_patch.strpath,
            remote_coverage_hook_patch.strpath)
        # See if we need to install the patch command:
        result = self.ipapp.ssh_client.run_command('rpm -q patch')
        if not result.success:
            # Setup yum repositories and install patch
            local_yum_repo = log_path.join('yum.local.repo')
            remote_yum_repo = '/etc/yum.repos.d/local.repo'
            repo_data = cfme_data['basic_info']['local_yum_repo']
            yum_repo_data = '''
[{name}]
name={name}
baseurl={baseurl}
enabled={enabled}
gpgcheck={gpgcheck}
'''.format(
                name=repo_data['name'],
                baseurl=repo_data['baseurl'],
                enabled=repo_data['enabled'],
                gpgcheck=repo_data['gpgcheck'])
            with open(local_yum_repo.strpath, 'w') as f:
                f.write(yum_repo_data)
            self.ipapp.ssh_client.put_file(local_yum_repo.strpath, remote_yum_repo)
            self.ipapp.ssh_client.run_command('yum install -y patch')
            # Remove the yum repo just in case a test of registering the system might
            # happen and this repo cause problems with the test.
            self.ipapp.ssh_client.run_command('rm {}'.format(remote_yum_repo))
        # patch system.
        result = self.ipapp.ssh_client.run_command('cd {}; patch -p1 < {}'.format(
            rails_root.strpath,
            remote_coverage_hook_patch.strpath))
        return result.success

    def _collect_reports(self):
        # restart evm to stop the proccesses and let the simplecov exit hook run
        self.ipapp.ssh_client.run_command('systemctl stop evmserverd')
        # collect back to the collection appliance if parallelized
        if store.current_appliance != self.collection_appliance:
            self.print_message('sending reports to {}'.format(self.collection_appliance.hostname))
            result = self.ipapp.ssh_client.run_command(
                'sshpass -p {passwd} '
                'scp -o StrictHostKeyChecking=no '
                '-r /var/www/miq/vmdb/coverage/* '
                '{addr}:/var/www/miq/vmdb/coverage/'.format(
                    addr=self.collection_appliance.hostname,
                    passwd=quote(self.ipapp.ssh_client._connect_kwargs['password'])),
                timeout=1800)
            if not result:
                self.print_message('There was an error sending reports: ' + str(result))

    def _retrieve_coverage_reports(self):
        # Before merging, archive and collect all the raw coverage results
        ssh_client = self.collection_appliance.ssh_client
        ssh_client.run_command('cd /var/www/miq/vmdb/;'
            'tar czf /tmp/ui-coverage-raw.tgz coverage/')
        ssh_client.get_file('/tmp/ui-coverage-raw.tgz', coverage_results_archive.strpath)

    def _upload_coverage_merger(self):
        ssh_client = self.collection_appliance.ssh_client
        ssh_client.put_file(coverage_merger.strpath, rails_root.strpath)

    def _merge_coverage_reports(self):
        # run the merger on the appliance to generate the simplecov report
        # This has been failing, presumably due to oom errors :(
        self._upload_coverage_merger()
        ssh_client = self.collection_appliance.ssh_client
        ssh_client.run_rails_command(coverage_merger.basename)

    def _retrieve_merged_reports(self):
        # Now bring the report back (tar it, get it, untar it)
        ssh_client = self.collection_appliance.ssh_client
        ssh_client.run_command('cd /var/www/miq/vmdb/coverage;'
            'tar czf /tmp/ui-coverage-results.tgz merged/')
        ssh_client.get_file('/tmp/ui-coverage-results.tgz', coverage_results_archive.strpath)
        subprocess.Popen(['/usr/bin/env', 'tar', '-xaf', coverage_results_archive.strpath,
            '-C', coverage_output_dir.strpath]).wait()


class UiCoveragePlugin(object):
    def pytest_configure(self, config):
        # cleanup cruft from previous runs
        if store.parallelizer_role != 'slave':
            clean_coverage_dir()
        coverage_appliance_conf.check() and coverage_appliance_conf.remove()

    def pytest_sessionstart(self, session):
        # master knows all the appliance URLs now, so name the first one as our
        # report recipient for merging at the end. Need to to write this out to a conf file
        # since all the slaves are going to use to to know where to ship their reports
        if store.parallelizer_role == 'master':
            collection_appliance_address = manager().collection_appliance.hostname
            conf.runtime['.ui-coverage']['collection_appliance'] = collection_appliance_address
            conf.save('.ui-coverage')

    @pytest.mark.hookwrapper
    def pytest_collection_finish(self):
        yield
        # Install coverage after collection finishes
        if store.parallelizer_role != 'master':
            manager().install()

    def pytest_sessionfinish(self, exitstatus):
        # Now master/standalone needs to move all the reports to an appliance for the source report
        if store.parallelizer_role != 'master':
            manager().collect()

        # for slaves, everything is done at this point
        if store.parallelizer_role == 'slave':
            return

        # on master/standalone, merge all the collected reports and bring them back
        manager().merge()

# TODO
# When the coverage reporting breaks out, we'll want to have this handy,
# so I'm commenting it out instead of outright deleting it :)
#         try:
#             global ui_coverage_percent
#             last_run = json.load(log_path.join('coverage', 'merged', '.last_run.json').open())
#             ui_coverage_percent = last_run['result']['covered_percent']
#             style = {'bold': True}
#             if ui_coverage_percent > 40:
#                 style['green'] = True
#             else:
#                 style['red'] = True
#             store.write_line('UI Coverage Result: {}%'.format(ui_coverage_percent),
#                 **style)
#         except Exception as ex:
#             logger.error('Error printing coverage report to terminal')
#             logger.exception(ex)


def pytest_addoption(parser):
    group = parser.getgroup('cfme')
    group.addoption('--ui-coverage', dest='ui_coverage', action='store_true', default=False,
        help="Enable setup and collection of ui coverage on an appliance")


def pytest_cmdline_main(config):
    # Only register the plugin worker if ui coverage is enabled
    if config.option.ui_coverage:
        config.pluginmanager.register(UiCoveragePlugin(), name="ui-coverage")
