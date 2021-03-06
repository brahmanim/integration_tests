from cached_property import cached_property

from . import ContainersProvider
from utils.varmeth import variable
from utils.path import data_path
from os import path
from wrapanapi.openshift import Openshift
from utils.ocp_cli import OcpCli


class CustomAttribute(object):
    def __init__(self, name, value, field_type=None, href=None):
        self.name = name
        self.value = value
        self.field_type = field_type
        self.href = href


class OpenshiftProvider(ContainersProvider):
    num_route = ['num_route']
    STATS_TO_MATCH = ContainersProvider.STATS_TO_MATCH + num_route
    type_name = "openshift"
    mgmt_class = Openshift
    db_types = ["Openshift::ContainerManager"]

    def __init__(self, name=None, credentials=None, key=None, zone=None,
                 hawkular=False, hostname=None, api_port=None, sec_protocol=None,
                 hawkular_sec_protocol=None, provider_data=None,
                 appliance=None, hawkular_hostname=None, hawkular_api_port=None):
        super(OpenshiftProvider, self).__init__(
            name=name, credentials=credentials, key=key, zone=zone, hawkular=hawkular,
            hostname=hostname, api_port=api_port, sec_protocol=sec_protocol,
            hawkular_sec_protocol=hawkular_sec_protocol, provider_data=provider_data,
            appliance=appliance, hawkular_hostname=hawkular_hostname,
            hawkular_api_port=hawkular_api_port)

    @cached_property
    def cli(self):
        return OcpCli(self)

    def href(self):
        return self.appliance.rest_api.collections.providers\
            .find_by(name=self.name).resources[0].href

    def _form_mapping(self, create=None, **kwargs):
        hawkular = kwargs.get('hawkular')
        sec_protocol = kwargs.get('sec_protocol')
        hawkular_hostname = kwargs.get('hawkular_hostname')
        hawkular_sec_protocol = kwargs.get('hawkular_sec_protocol')
        default_ca_certificate = self.get_cert()
        hawkular_ca_certificate = default_ca_certificate
        if not sec_protocol == 'SSL trusting custom CA':
            default_ca_certificate = None
        if not hawkular_sec_protocol == 'SSL trusting custom CA':
            hawkular_ca_certificate = None
        if self.appliance.version > '5.8.0.3' and hawkular:
            sec_protocol = sec_protocol
            hawkular_hostname = hawkular_hostname
            hawkular_sec_protocol = hawkular_sec_protocol
        elif self.appliance.version > '5.8.0.3' and not hawkular:
            sec_protocol = sec_protocol
            hawkular_hostname = None
            hawkular_sec_protocol = None
        else:
            sec_protocol = None
            hawkular_hostname = None
            hawkular_sec_protocol = None
        return {'name_text': kwargs.get('name'),
                'type_select': create and 'OpenShift',
                'hostname_text': kwargs.get('hostname'),
                'port_text': kwargs.get('api_port'),
                'sec_protocol': sec_protocol,
                'zone_select': kwargs.get('zone'),
                'hawkular_hostname': hawkular_hostname,
                'hawkular_api_port': kwargs.get('hawkular_api_port'),
                'hawkular_sec_protocol': hawkular_sec_protocol,
                'trusted_ca_certificates': default_ca_certificate,
                'hawkular_ca_certificates': hawkular_ca_certificate
                }

    @variable(alias='db')
    def num_route(self):
        return self._num_db_generic('container_routes')

    @num_route.variant('ui')
    def num_route_ui(self):
        return int(self.get_detail("Relationships", "Routes"))

    @variable(alias='db')
    def num_template(self):
        return self._num_db_generic('container_templates')

    @num_template.variant('ui')
    def num_template_ui(self):
        return int(self.get_detail("Relationships", "Container Templates"))

    @staticmethod
    def from_config(prov_config, prov_key, appliance=None):
        token_creds = OpenshiftProvider.process_credential_yaml_key(
            prov_config['credentials'], cred_type='token')
        return OpenshiftProvider(
            name=prov_config['name'],
            credentials={'token': token_creds},
            key=prov_key,
            zone=prov_config['server_zone'],
            hostname=prov_config['endpoints']['default'].hostname or prov_config['ipaddress'],
            api_port=prov_config['endpoints']['default'].api_port,
            sec_protocol=prov_config['endpoints']['default'].sec_protocol,
            hawkular_sec_protocol=prov_config['endpoints']['hawkular'].sec_protocol,
            hawkular_hostname=prov_config['endpoints']['hawkular'].hostname,
            hawkular_api_port=prov_config['endpoints']['hawkular'].api_port,
            provider_data=prov_config,
            appliance=appliance)

    def custom_attributes(self):
        """returns custom attributes"""
        response = self.appliance.rest_api.get(
            path.join(self.href(), 'custom_attributes'))
        out = []
        for attr_dict in response['resources']:
            attr = self.appliance.rest_api.get(attr_dict['href'])
            out.append(
                CustomAttribute(
                    attr['name'], attr['value'],
                    (attr['field_type'] if 'field_type' in attr else None),
                    attr_dict['href']
                )
            )
        return out

    def add_custom_attributes(self, *custom_attributes):
        """Adding static custom attributes to provider.
        Args:
            custom_attributes: The custom attributes to add.
        returns: response.
        """
        if not custom_attributes:
            raise TypeError('{} takes at least 1 argument.'
                            .format(self.add_custom_attributes.__name__))
        for attr in custom_attributes:
            if not isinstance(attr, CustomAttribute):
                raise TypeError('All arguments should be of type {}. ({} != {})'
                                .format(CustomAttribute, type(attr), CustomAttribute))
        payload = {
            "action": "add",
            "resources": [{
                "name": ca.name,
                "value": str(ca.value)
            } for ca in custom_attributes]}
        for i, fld_tp in enumerate([attr.field_type for attr in custom_attributes]):
            if fld_tp:
                payload['resources'][i]['field_type'] = fld_tp
        return self.appliance.rest_api.post(
            path.join(self.href(), 'custom_attributes'), **payload)

    def edit_custom_attributes(self, *custom_attributes):
        """Editing static custom attributes in provider.
        Args:
            custom_attributes: The custom attributes to edit.
        returns: response.
        """
        if not custom_attributes:
            raise TypeError('{} takes at least 1 argument.'
                            .format(self.edit_custom_attributes.__name__))
        for attr in custom_attributes:
            if not isinstance(attr, CustomAttribute):
                raise TypeError('All arguments should be of type {}. ({} != {})'
                                .format(CustomAttribute, type(attr), CustomAttribute))
        attribs = self.custom_attributes()
        payload = {
            "action": "edit",
            "resources": [{
                "href": filter(lambda attr: attr.name == ca.name, attribs)[-1].href,
                "value": ca.value
            } for ca in custom_attributes]}
        return self.appliance.rest_api.post(
            path.join(self.href(), 'custom_attributes'), **payload)

    def delete_custom_attributes(self, *custom_attributes):
        """Deleting static custom attributes from provider.
        Args:
            custom_attributes: The custom attributes to delete.
                               (Could be also names (str))
        returns: response.
        """
        names = []
        for attr in custom_attributes:
            attr_type = type(attr)
            if attr_type in (str, CustomAttribute):
                names.append(attr if attr_type is str else attr.name)
            else:
                raise TypeError('Type of arguments should be either'
                                'str or CustomAttribute. ({} not in [str, CustomAttribute])'
                                .format(type(attr)))
        attribs = self.custom_attributes()
        if not names:
            names = [attr.name for attr in attribs]
        payload = {
            "action": "delete",
            "resources": [{
                "href": attr.href,
            } for attr in attribs if attr.name in names]}
        return self.appliance.rest_api.post(
            path.join(self.href(), 'custom_attributes'), **payload)

    @staticmethod
    def get_cert():
        """Getting OpenShift's certificate from the master machine.
        Args:
           No args.
        returns: certificate's content.
        """
        cert_file_path = path.join(str(data_path), 'cert-auths', 'cmqe-tests-openshift-signer.crt')
        with open(cert_file_path) as f:
            return f.read()
