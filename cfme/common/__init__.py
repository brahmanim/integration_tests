# -*- coding: utf-8 -*-
from functools import partial
from urlparse import urlparse

from cached_property import cached_property
from cfme.configure.configuration import Category, Tag
from cfme.fixtures import pytest_selenium as sel
from cfme.web_ui import CheckboxTree, BootstrapTreeview, flash, form_buttons, mixins, toolbar
from cfme.web_ui.timelines import Timelines
from cfme.web_ui.topology import Topology
from cfme.web_ui.utilization import Utilization
from sqlalchemy.orm import aliased
from utils import attributize_string, version, deferred_verpick
from utils.units import Unit
from utils.varmeth import variable
from utils.log import logger

pol_btn = partial(toolbar.select, "Policy")


class PolicyProfileAssignable(object):
    """This class can be inherited by anything that provider load_details method.

    It provides functionality to assign and unassign Policy Profiles"""
    manage_policies_tree = deferred_verpick({
        version.LOWEST: CheckboxTree("//div[@id='protect_treebox']/ul"),
        "5.7": BootstrapTreeview("protectbox")
    })

    @property
    def assigned_policy_profiles(self):
        try:
            return self._assigned_policy_profiles
        except AttributeError:
            self._assigned_policy_profiles = set([])
            return self._assigned_policy_profiles

    def assign_policy_profiles(self, *policy_profile_names):
        """ Assign Policy Profiles to this object.

        Args:
            policy_profile_names: :py:class:`str` with Policy Profile names. After Control/Explorer
                coverage goes in, PolicyProfile objects will be also passable.
        """
        map(self.assigned_policy_profiles.add, policy_profile_names)
        self._assign_unassign_policy_profiles(True, *policy_profile_names)

    def unassign_policy_profiles(self, *policy_profile_names):
        """ Unssign Policy Profiles to this object.

        Args:
            policy_profile_names: :py:class:`str` with Policy Profile names. After Control/Explorer
                coverage goes in, PolicyProfile objects will be also passable.
        """
        for pp_name in policy_profile_names:
            try:
                self.assigned_policy_profiles.remove(pp_name)
            except KeyError:
                pass
        self._assign_unassign_policy_profiles(False, *policy_profile_names)

    def _assign_unassign_policy_profiles(self, assign, *policy_profile_names):
        """DRY function for managing policy profiles.

        See :py:func:`assign_policy_profiles` and :py:func:`assign_policy_profiles`

        Args:
            assign: Wheter to assign or unassign.
            policy_profile_names: :py:class:`str` with Policy Profile names.
        """
        self.load_details(refresh=True)
        pol_btn("Manage Policies")
        for policy_profile in policy_profile_names:
            if assign:
                self.manage_policies_tree.check_node(policy_profile)
            else:
                self.manage_policies_tree.uncheck_node(policy_profile)
        form_buttons.save()
        flash.assert_no_errors()


class Taggable(object):
    """This class can be inherited by anything that provider load_details method.

    It provides functionality to assign and unassign tags."""
    def add_tag(self, tag, single_value=False):
        self.load_details(refresh=True)
        mixins.add_tag(tag, single_value=single_value, navigate=True)

    def add_tags(self, tags):
        """Add list of tags
        tags: List of `Tag`
        """
        for tag in tags:
            self.add_tag(tag=tag)

    def remove_tag(self, tag):
        self.load_details(refresh=True)
        mixins.remove_tag(tag)

    def remove_tags(self, tags):
        """Remove list of tags
        tags: List of `Tag`
        """
        for tag in tags:
            self.remove_tag(tag=tag)

    @variable(alias='ui')
    def get_tags(self, tag="My Company Tags"):
        self.load_details(refresh=True)
        tags = []
        # Sample out put from UI, [u'Department: Accounting | Engineering', u'Location: London']
        for _tag in mixins.get_tags(tag=tag):
            if _tag == 'No {} have been assigned'.format(tag):
                return tags
            _tag = _tag.split(':', 1)
            if len(_tag) != 2:
                raise RuntimeError('Unknown format of tagging in UI [{}]'.format(_tag))
            if ' | ' in _tag[1]:
                for _sub_tag in _tag[1].split(' | '):
                    tags.append(Tag(category=Category(display_name=tag[0], single_value=None),
                                    display_name=_sub_tag.strip()))
            else:
                tags.append(Tag(category=Category(display_name=_tag[0], single_value=None),
                                display_name=_tag[1].strip()))
        return tags

    @get_tags.variant('db')
    def get_tags_db(self):
        """
        Gets tags detail from database
        Column order: `tag_id`, `db_id`, `category`, `tag_name`, `single_value`
        """
        # Some times object of db_id might changed in database, when we do CRUD operations,
        # do update now
        self.load_details(refresh=True)
        if not self.db_id or not self.taggable_type:
            raise KeyError("'db_id' and/or 'taggable_type' not set")
        t_cls1 = aliased(self.appliance.db['classifications'])
        t_cls2 = aliased(self.appliance.db['classifications'])
        t_tgg = aliased(self.appliance.db['taggings'])
        query = self.appliance.db.session.query(t_cls1.tag_id, t_tgg.taggable_id.label('db_id'),
                                       t_cls2.description.label('category'),
                                       t_cls1.description.label('tag_name'), t_cls1.single_value)\
            .join(t_cls2, t_cls1.parent_id == t_cls2.id)\
            .join(t_tgg, t_tgg.tag_id == t_cls1.tag_id)\
            .filter(t_tgg.taggable_id == self.db_id)\
            .filter(t_tgg.taggable_type == self.taggable_type)
        tags = []
        for tag in query.all():
            tags.append(Tag(category=Category(display_name=tag.category,
                                              single_value=tag.single_value),
                            display_name=tag.tag_name))
        return tags


class SummaryMixin(object):
    """Use this mixin to have simple access to the Summary informations of an object.

    Requires that the class has ``load_details(refresh)`` method defined.

    All the names from the UI are "attributized".

    Sample usage:

    .. code-block:: python

        # You can retrieve the text value as it is in the UI
        provider.summary.properties.host_name.text_value  # => 'hostname'
        # Or let it guess if it is a number and return float or int
        provider.summary.properties.aggregate_host_cpus.value  # => 12
        # You can get the image address
        provider.summary.foo.bar.img  # => value parsed by urlparse()
        # Or the onclick link
        provider.summary.foo.bar.link  # => 'http://foo/bar'
        # Check if it is clickable
        assert provider.summary.xyz.qwer.clickable

        # You can iterate like it was a dictionary
        for table_name, table in provider.summary:
            # table_name contains title of the table
            for key, value in table:
                # key contains the left cell text, value contains the value holder
                print('{}: {}'.format(key, value.text_value))


    """
    @cached_property
    def summary(self):
        return Summary(self)


class Summary(object):
    """Summary container class. An entry point to the summary listing"""
    HEADERS = '//th[@align="left"]'

    def __init__(self, o):
        self._object = o
        self._keys = []
        self.reload()

    def __repr__(self):
        return "<Summary {}>".format(" ".join(self._keys))

    def reload(self):
        for key in self._keys:
            try:
                delattr(self, key)
            except AttributeError:
                pass
        self._keys = []
        self._object.load_details(refresh=True)
        for header in sel.elements(self.HEADERS):
            header_text = sel.text_sane(header)
            header_id = attributize_string(header_text)
            table_object = SummaryTable(self._object, header_text, header)
            setattr(self, header_id, table_object)
            self._keys.append(header_id)

    def __iter__(self):
        """This enables you to iterate through like it was a dictionary, just without .iteritems"""
        for key in self._keys:
            yield (key, getattr(self, key))

    def groups(self):
        """Returns a dictionary of keys (table titles) and table objects."""
        return dict(iter(self))

    @property
    def group_names(self):
        """Returns names of the tables."""
        return self._keys


class SummaryTable(object):
    ROWS = '../../../tbody/tr'

    MULTIKEY_LOC = '../../../tbody/tr[1]/td/strong'

    def __init__(self, o, text, entry, skip_load=False):
        self._object = o
        self._text = text
        self._entry = entry
        self._raw_keys = []
        self._keys = []
        self._multitable = False
        if not skip_load:
            self.load()
        else:
            logger.warning(
                "Child SummaryTable created for {table_name}, "
                "this table wasn't initialized due to skip_load value".format(
                    table_name=self._text))

    def __repr__(self):
        if self._multitable:
            return "<SummaryTable {main_table_name}:\n\t {sub_tables}>".format(
                main_table_name=self._text,
                sub_tables='\n\t'.join([repr(getattr(self, key)) for key in self._keys]))

        return "<SummaryTable {} {}>".format(
            repr(self._text),
            " ".join("{}={}".format(key, repr(getattr(self, key))) for key in self._keys))

    def load(self):
        self._raw_keys = []
        self._keys = []
        key_values = []
        if sel.is_displayed(self.MULTIKEY_LOC, root=self._entry):
            logger.warning(
                "Parent SummaryTable created for {table_name}, "
                "it might create few un-initialized SummaryTable".format(
                    table_name=self._text))
            self._multitable = True
            # get all table rows (include titles)
            table_rows = sel.elements(self.ROWS, root=self._entry)

            # parsing table titles
            table_titles = sel.elements('./td', root=table_rows[0])
            table_titles_text = [el.text.replace(" ", "_") for el in table_titles]

            # match each line values with the relevant title
            for row in table_rows[1:]:
                # creating mapping between title and row values
                row_mapping = dict(zip(table_titles_text,
                                       [el.text for el in sel.elements('./td', root=row)]))

                # set the value of the "name" column to be the key of the entire table,
                # if "name" is not available setting the most left element to be the key
                row_key = row_mapping.get("Name", row_mapping.keys()[0])

                # creating empty table to populate the row data as regular table
                table = SummaryTable(self._object,
                                     row_key,
                                     row, skip_load=True)
                # set the keys of the table to table object
                table._keys = row_mapping.keys()

                # add attr for each key
                for key in row_mapping.keys():
                    setattr(table, key, row_mapping[key])

                # add the entire table to parent table keys
                self._keys.append(row_key)

                # add attr to parent table
                setattr(self, row_key, table)
            return

        for row in sel.elements(self.ROWS, root=self._entry):
            tds = sel.elements('./td', root=row)
            key = tds[0]
            klass = sel.get_attribute(key, 'class')
            if klass and 'label' in klass:
                # Ordinary field
                key_id = attributize_string(sel.text_sane(key))
                value = tuple(tds[1:])
                try:
                    rowspan = int(sel.get_attribute(key, 'rowspan'))
                except (ValueError, TypeError):
                    rowspan = None
                if rowspan:
                    key_values.append((key, key_id, [value]))
                else:
                    key_values.append((key, key_id, value))
            else:
                # value of last key_values should be extended
                key_values[-1][2].append(tuple(tds))
        for key, key_id, value in key_values:
            value_object = process_field(value)
            setattr(self, key_id, value_object)
            self._raw_keys.append(sel.text_sane(key))
            self._keys.append(key_id)

    def reload(self):
        self._object.load_details(refresh=True)
        for key in self._keys:
            try:
                delattr(self, key)
            except AttributeError:
                pass
        return self.load()

    @property
    def raw_keys(self):
        return self._raw_keys

    @property
    def keys(self):
        return self._keys

    def __iter__(self):
        for key in self._keys:
            yield (key, getattr(self, key))

    def items(self):
        return dict(iter(self))


class SummaryValue(object):
    def __init__(self, el):
        self._el = el

    def __repr__(self):
        return repr(self.text_value)

    @cached_property
    def img(self):
        try:
            img_o = sel.element('./img', root=self._el)
            return urlparse(sel.get_attribute(img_o, 'src').strip())
        except sel.NoSuchElementException:
            return None

    @cached_property
    def text_value(self):
        return sel.text_sane(self._el)

    @cached_property
    def value(self):
        # Try parsing a number
        try:
            return int(self.text_value)
        except (ValueError, TypeError):
            try:
                return float(self.text_value)
            except (ValueError, TypeError):
                try:
                    return Unit.parse(self.text_value)
                except ValueError:
                    return self.text_value

    @cached_property
    def link(self):
        if sel.get_attribute(sel.element('..', root=self._el), 'onclick'):
            return self._el
        else:
            return None

    @property
    def clickable(self):
        return self.link is not None

    def click(self):
        """A convenience function to click the summary item."""
        return sel.click(self)

    def _custom_click_handler(self, wait_ajax):
        if not self.clickable:
            raise ValueError("Cannot click on {} because it is not clickable".format(repr(self)))
        try:
            return sel.click(self.link, wait_ajax, no_custom_handler=True)
        except sel.StaleElementReferenceException:
            raise RuntimeError('Couldnt click on {} because the page was left.'.format(repr(self)))


def process_field(values):
    if isinstance(values, list):
        return map(process_field, values)
    else:
        if len(values) == 1:
            return SummaryValue(values[0])
        else:
            return map(SummaryValue, values)


class Validatable(SummaryMixin):
    """Mixin for various validations. Requires the class to be also :py:class:`Taggable`.

    :var :py:attr:`property_tuples`: Tuples which first value is the provider class's attribute
        name, the second value is provider's UI summary page field key. Should have values in
        child classes.
    """
    property_tuples = []

    def validate_properties(self):
        """Validation method which checks whether class attributes, which were used during creation
        of provider, are correctly displayed in Properties section of provider UI.

        The maps between class attribute and UI property is done via 'property_tuples' variable.

        Fails if some property does not match.
        """
        self.load_details(refresh=False)
        for property_tuple in self.property_tuples:
            expected_value = str(getattr(self, property_tuple[0], ''))
            shown_value = self.get_detail("Properties", property_tuple[1])
            assert expected_value == shown_value,\
                ("Property '{}' has wrong value, expected '{}' but was '{}'"
                 .format(property_tuple, expected_value, shown_value))

    def validate_tags(self, tag="My Company Tags", reference_tags=None):
        """Validation method which check tagging between UI and database.

        To use this method, `self`/`caller` should be extended with `Taggable` class

        Args:
            tag: tag name, default is `My Company Tags`
            reference_tags: If you want to compare user input with database, pass user input
                as `reference_tags`
        """
        if reference_tags and not isinstance(reference_tags, list):
            raise KeyError("'reference_tags' should be an instance of list")
        # Get tags from UI and DB
        tags_ui = self.get_tags(method='ui')
        tags_db = self.get_tags(method='db')
        # Verify tags
        assert len(tags_db) == len(tags_ui), \
            ("Tags count between DB and UI mismatch, expected '{}' but was '{}'"
             .format(tags_db, tags_ui))
        if len(tags_ui) > 0:
            tags_ui = sorted(tags_ui, key=lambda x: (x.category.display_name, x.display_name))
            tags_db = sorted(tags_db, key=lambda x: (x.category.display_name, x.display_name))
            for i in range(len(tags_db)):
                assert \
                    tags_db[i].category.display_name == tags_ui[i].category.display_name,\
                    ("Expected category '{}', but was '{}'".format(
                        tags_db[i].category.display_name,
                        tags_ui[i].category.display_name))
                assert tags_db[i].display_name == tags_ui[i].display_name, \
                    ("Expected tag_name '{}', but was '{}'".format(tags_db[i].display_name,
                                                                   tags_ui[i].display_name))
        # if user passed reference tags, validate with database
        if reference_tags:
            for ref_tag in reference_tags:
                found = False
                for d_tag in tags_db:
                    if ref_tag.category.display_name == d_tag.category.display_name \
                            and ref_tag.display_name == d_tag.display_name:
                        found = True
                        assert ref_tag.category.single_value == d_tag.category.single_value, \
                            ("'single_value' of '{}' did not match'"
                             .format(ref_tag))
                assert found, ("Tag '{}' not found in database".format(ref_tag))


class TopologyMixin(object):
    """Use this mixin to have simple access to the Topology page.
    To use this `TopologyMixin` you have to implement `load_topology_page`
    function, which should take to topology page

    Sample usage:

    .. code-block:: python

        # You can retrieve the elements details as it is in the UI
        topology.elements  # => 'hostname'
        # You can do actions on topology page
        topology.display_names.enable()
        topology.display_names.disable()
        topology.display_names.is_enabled
        # You can do actions on topology search box
        topology.search_box.text(text='hello')
        topology.search_box.text(text='hello', submit=False)
        topology.search_box.submit()
        topology.search_box.clear()
        # You can get legends and can perform actions
        topology.legends
        topology.pod.name
        topology.pod.is_active
        topology.pod.set_active()
        # You can get elements, element parents and children
        topology.elements
        topology.elements[0].parents
        topology.elements[0].children
        topology.elements[0].double_click()
        topology.elements[0].is_displayed()

    """
    @cached_property
    def topology(self):
        return Topology(self)


class TimelinesMixin(object):
    """Use this mixin to have simple access to the Timelines page.
    To use this `TimelinesMixin` you have to implement `load_timelines_page`
    function, which should take to timelines page

    Sample usage:

    .. code-block:: python

        # Change Timelines showing interval Select
        timelines.change_interval('Hourly')
        # Change Timelines showing event group Select
        timelines.change_event_groups('Application')
        # Change Level of showed Timelines
        timelines.change_level('Detail')
        # Check whether timelines contain particular event
        # which is generated after provided datetime
        timelines.contains_event('hawkular_deployment.ok', before_test_date)

    """
    @cached_property
    def timelines(self):
        return Timelines(self)


class UtilizationMixin(object):
    """Use this mixin to have simple access to the Utilization information of an object.

    Requires that the class(page) has ``load_details(refresh)`` method
    and ``taggable_type`` should be defined.

    All the chart names from the UI are "attributized".

    Sample usage:
    .. code-block:: python

        # You can list available charts
        page.utilization.charts  # => '[ 'jvm_heap_usage_bytes','web_sessions','transactions']'
        # You can get the data from chart
        page.utilization.jvm_heap_usage_bytes.list_data_chart()  # => returns data as list
        # You can get the data from table
        provider.utilization.jvm_heap_usage_bytes.list_data_table()  # => returns data as list
        # You can get the data from wrapanapi
        page.utilization.jvm_heap_usage_bytes.list_data_mgmt()  # => returns data as list
        # You can change chart option
        page.utilization.jvm_non_heap_usage_bytes.option.set_by_visible_text(op_interval='Daily')
        # You can list available ledgends
        page.utilization.jvm_non_heap_usage_bytes.legends
        # You can enable/disable legends
        page.utilization.jvm_non_heap_usage_bytes.committed.set_active(active=False) # => Disables
        page.utilization.jvm_non_heap_usage_bytes.committed.set_active(active=True) # => Enables
    """
    @cached_property
    def utilization(self):
        return Utilization(self)
