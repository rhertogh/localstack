import logging
import traceback
import requests.models
from flask import Flask, request
from localstack.utils.aws import aws_stack
from localstack.utils.common import (
    parse_request_data, short_uid, long_uid, clone, select_attributes, timestamp_millis)
from localstack.utils.cloudformation import template_deployer
from localstack.utils.aws.aws_responses import (
    requests_response_xml, requests_to_flask_response, flask_error_response_xml)
from localstack.services.cloudformation import cloudformation_listener  # , cloudformation_starter

APP_NAME = 'cloudformation_api'
app = Flask(APP_NAME)

LOG = logging.getLogger(__name__)

XMLNS_CF = 'http://cloudformation.amazonaws.com/doc/2010-05-15/'


class RegionState(object):
    STATES = {}

    def __init__(self):
        # maps stack ID to stack details
        self.stacks = {}

    @classmethod
    def get(cls):
        region = aws_stack.get_region()
        state = cls.STATES[region] = cls.STATES.get(region) or RegionState()
        return state

    @property
    def exports(self):
        exports = []
        output_keys = {}
        for stack_id, stack in self.stacks.items():
            for output in stack.outputs:
                if 'ExportName' not in output:
                    continue
                export_name = output['ExportName']
                if export_name in output_keys:
                    # TODO: raise exception on stack creation in case of duplicate exports
                    LOG.warning('Found duplicate export name %s in stacks: %s %s' % (
                        export_name, output_keys[export_name], stack.stack_id))
                entry = {
                    'ExportingStackId': stack.stack_id,
                    'Name': export_name,
                    'Value': output['OutputValue']
                }
                exports.append(entry)
                output_keys[export_name] = stack.stack_id
        return exports


class Stack(object):

    def __init__(self, metadata=None, template={}):
        self.metadata = metadata or {}
        self.template = template or {}
        self._template_raw = clone(self.template)
        self.template_original = clone(self.template)
        # initialize resources
        for resource_id, resource in self.template_resources.items():
            resource['LogicalResourceId'] = self.template_original['Resources'][resource_id]['LogicalResourceId'] = (
                resource.get('LogicalResourceId') or resource_id)
        # initialize stack template attributes
        self.template['StackId'] = self.metadata['StackId'] = (self.metadata.get('StackId') or
            aws_stack.cloudformation_stack_arn(self.stack_name, short_uid()))
        self.template['Parameters'] = self.template.get('Parameters') or {}
        # initialize metadata
        self.metadata['Parameters'] = self.metadata.get('Parameters') or []
        self.metadata['StackStatus'] = 'CREATE_IN_PROGRESS'
        self.metadata['CreationTime'] = self.metadata.get('CreationTime') or timestamp_millis()
        # maps resource id to resource state
        self.resource_states = {}
        # maps resource id to moto resource class instance (TODO: remove in the future)
        self.moto_resource_statuses = {}
        # list of stack events
        self.events = []
        # list of stack change sets
        self.change_sets = []
        # initialize parameters
        for i in range(1, 100):
            key = 'Parameters.member.%s.ParameterKey' % i
            value = 'Parameters.member.%s.ParameterValue' % i
            key = self.metadata.get(key)
            value = self.metadata.get(value)
            if not key:
                break
            self.metadata['Parameters'].append({'ParameterKey': key, 'ParameterValue': value})

    def describe_details(self):
        attrs = ['StackId', 'StackName', 'Description', 'Parameters', 'StackStatusReason',
            'StackStatus', 'Capabilities', 'Outputs', 'Tags', 'ParentId', 'RootId', 'RoleARN',
            'CreationTime', 'DeletionTime', 'LastUpdatedTime', 'ChangeSetId']
        result = select_attributes(self.metadata, attrs)
        for attr in ['Capabilities']:
            result[attr] = {'member': result.get(attr, [])}
        result['Outputs'] = {'member': self.outputs}
        result['Parameters'] = {'member': self.stack_parameters()}
        return result

    def set_stack_status(self, status):
        self.metadata['StackStatus'] = status
        event = {
            'EventId': long_uid(),
            'Timestamp': timestamp_millis(),
            'StackId': self.stack_id,
            'StackName': self.stack_name,
            'LogicalResourceId': self.stack_name,
            'PhysicalResourceId': self.stack_id,
            'ResourceStatus': status,
            'ResourceType': 'AWS::CloudFormation::Stack'
        }
        self.events.insert(0, event)

    def set_resource_status(self, resource_id, status, physical_res_id=None):
        resource = self.resources[resource_id]
        state = self.resource_states[resource_id] = self.resource_states.get(resource_id) or {}
        attr_defaults = (('LogicalResourceId', resource_id), ('PhysicalResourceId', physical_res_id))
        for res in [resource, state]:
            for attr, default in attr_defaults:
                res[attr] = res.get(attr) or default
        state['ResourceStatus'] = status
        state['StackName'] = state.get('StackName') or self.stack_name
        state['StackId'] = state.get('StackId') or self.stack_id
        state['ResourceType'] = state.get('ResourceType') or self.resources[resource_id].get('Type')

    def resource_status(self, resource_id):
        result = self._lookup(self.resource_states, resource_id)
        return result

    @property
    def stack_name(self):
        return self.metadata['StackName']

    @property
    def stack_id(self):
        return self.metadata['StackId']

    @property
    def resources(self):
        """ Return dict of resources, parameters, conditions, and other stack metadata. """
        def add_params(defaults=True):
            for param in self.stack_parameters(defaults=defaults):
                if param['ParameterKey'] not in result:
                    props = {'Value': param['ParameterValue']}
                    result[param['ParameterKey']] = {'Type': 'Parameter',
                        'LogicalResourceId': param['ParameterKey'], 'Properties': props}
        result = dict(self.template_resources)
        add_params(defaults=False)
        for name, value in self.conditions.items():
            if name not in result:
                result[name] = {'Type': 'Parameter', 'LogicalResourceId': name, 'Properties': {'Value': value}}
        for name, value in self.mappings.items():
            if name not in result:
                result[name] = {'Type': 'Parameter', 'LogicalResourceId': name, 'Properties': {'Value': value}}
        add_params(defaults=True)
        return result

    @property
    def template_resources(self):
        return self.template['Resources']

    @property
    def outputs(self):
        result = []
        for k, details in self.template.get('Outputs', {}).items():
            template_deployer.resolve_refs_recursively(self.stack_name, details, self.resources)
            export = details.get('Export', {}).get('Name')
            description = details.get('Description')
            entry = {'OutputKey': k, 'OutputValue': details['Value'], 'Description': description, 'ExportName': export}
            result.append(entry)
        return result

    def stack_parameters(self, defaults=True):
        result = {p['ParameterKey']: p for p in self.metadata['Parameters']}
        if defaults:
            for key, value in self.template_parameters.items():
                result[key] = result.get(key) or {'ParameterKey': key, 'ParameterValue': value.get('Default')}
        result = list(result.values())
        return result

    @property
    def template_parameters(self):
        return self.template['Parameters']

    @property
    def conditions(self):
        return self.template.get('Conditions', {})

    @property
    def mappings(self):
        return self.template.get('Mappings', {})

    @property
    def exports_map(self):
        result = {}
        for export in RegionState.get().exports:
            result[export['Name']] = export
        return result

    @property
    def status(self):
        return self.metadata['StackStatus']

    def resource(self, resource_id):
        return self._lookup(self.resources, resource_id)

    def _lookup(self, resource_map, resource_id):
        resource = resource_map.get(resource_id)
        if not resource:
            raise Exception('Unable to find details for resource "%s" in stack "%s"' % (resource_id, self.stack_name))
        return resource

    def copy(self):
        return Stack(metadata=dict(self.metadata), template=dict(self.template))


class StackChangeSet(Stack):

    def __init__(self, params={}, template={}):
        super(StackChangeSet, self).__init__(params, template)
        name = self.metadata['ChangeSetName']
        if not self.metadata.get('ChangeSetId'):
            self.metadata['ChangeSetId'] = aws_stack.cf_change_set_arn(name, change_set_id=short_uid())
        stack = self.stack = find_stack(self.metadata['StackName'])
        self.metadata['StackId'] = stack.stack_id
        self.metadata['Status'] = 'CREATE_PENDING'

    @property
    def change_set_id(self):
        return self.metadata['ChangeSetId']

    @property
    def change_set_name(self):
        return self.metadata['ChangeSetName']


# --------------
# API ENDPOINTS
# --------------

def create_stack(req_params):
    state = RegionState.get()
    cloudformation_listener.prepare_template_body(req_params)
    template = template_deployer.parse_template(req_params['TemplateBody'])
    template['StackName'] = req_params.get('StackName')
    stack = Stack(req_params, template)
    state.stacks[stack.stack_id] = stack
    deployer = template_deployer.TemplateDeployer(stack)
    try:
        deployer.deploy_stack()
    except Exception as e:
        stack.set_stack_status('CREATE_FAILED')
        msg = 'Unable to create stack "%s": %s' % (stack.stack_name, e)
        LOG.debug('%s %s' % (msg, traceback.format_exc()))
        return error_response(msg, code=400, code_string='ValidationError')
    result = {'StackId': stack.stack_id}
    return result


def delete_stack(req_params):
    state = RegionState.get()
    stack_name = req_params.get('StackName')
    stack = find_stack(stack_name)
    deployer = template_deployer.TemplateDeployer(stack)
    deployer.delete_stack()
    state.stacks.pop(stack.stack_id)
    return {}


def update_stack(req_params):
    stack_name = req_params.get('StackName')
    stack = find_stack(stack_name)
    if not stack:
        return error_response('Unable to update non-existing stack "%s"' % stack_name,
            code=404, code_string='ValidationError')
    cloudformation_listener.prepare_template_body(req_params)
    template = template_deployer.parse_template(req_params['TemplateBody'])
    new_stack = Stack(req_params, template)
    deployer = template_deployer.TemplateDeployer(stack)
    try:
        deployer.update_stack(new_stack)
    except Exception as e:
        stack.set_stack_status('UPDATE_FAILED')
        msg = 'Unable to update stack "%s": %s' % (stack_name, e)
        LOG.debug('%s %s' % (msg, traceback.format_exc()))
        return error_response(msg, code=400, code_string='ValidationError')
    result = {'StackId': stack.stack_id}
    return result


def describe_stacks(req_params):
    state = RegionState.get()
    stack_name = req_params.get('StackName')
    stacks = [s.describe_details() for s in state.stacks.values() if stack_name in [None, s.stack_name]]
    if stack_name and not stacks:
        return error_response('Stack with id %s does not exist' % stack_name,
            code=400, code_string='ValidationError')
    result = {'Stacks': {'member': stacks}}
    return result


def list_stacks(req_params):
    state = RegionState.get()
    filter = req_params.get('StackStatusFilter')
    stacks = [s.describe_details() for s in state.stacks.values() if filter in [None, s.status]]
    attrs = ['StackId', 'StackName', 'TemplateDescription', 'CreationTime', 'LastUpdatedTime', 'DeletionTime',
        'StackStatus', 'StackStatusReason', 'ParentId', 'RootId', 'DriftInformation']
    stacks = [select_attributes(stack, attrs) for stack in stacks]
    result = {'StackSummaries': {'member': stacks}}
    return result


def describe_stack_resource(req_params):
    stack_name = req_params.get('StackName')
    resource_id = req_params.get('LogicalResourceId')
    stack = find_stack(stack_name)
    if not stack:
        return error_response('Unable to find stack named "%s"' % stack_name,
            code=404, code_string='ResourceNotFoundException')
    details = stack.resource_status(resource_id)
    result = {'StackResourceDetail': details}
    return result


def describe_stack_resources(req_params):
    stack_name = req_params.get('StackName')
    resource_id = req_params.get('LogicalResourceId')
    phys_resource_id = req_params.get('PhysicalResourceId')
    if phys_resource_id and stack_name:
        return error_response('Cannot specify both StackName and PhysicalResourceId')
    # TODO: filter stack by PhysicalResourceId!
    stack = find_stack(stack_name)
    statuses = [stack.resource_status(res_id) for res_id, _ in stack.resource_states.items() if
        resource_id in [res_id, None]]
    return {'StackResources': {'member': statuses}}


def list_stack_resources(req_params):
    result = describe_stack_resources(req_params)
    if not isinstance(result, dict):
        return result
    result = {'StackResourceSummaries': result.pop('StackResources')}
    return result


def create_change_set(req_params):
    stack_name = req_params.get('StackName')
    cloudformation_listener.prepare_template_body(req_params)
    template = template_deployer.parse_template(req_params['TemplateBody'])
    template['StackName'] = stack_name
    template['ChangeSetName'] = req_params.get('ChangeSetName')
    stack = existing = find_stack(stack_name)
    if not existing:
        # automatically create (empty) stack if none exists yet
        state = RegionState.get()
        empty_stack_template = dict(template)
        empty_stack_template['Resources'] = {}
        stack = Stack(req_params, empty_stack_template)
        state.stacks[stack.stack_id] = stack
    change_set = StackChangeSet(req_params, template)
    stack.change_sets.append(change_set)
    return {'StackId': change_set.stack_id, 'Id': change_set.change_set_id}


def execute_change_set(req_params):
    stack_name = req_params.get('StackName')
    cs_name = req_params.get('ChangeSetName')
    change_set = find_change_set(cs_name, stack_name=stack_name)
    if not change_set:
        return error_response('Unable to find change set "%s" for stack "%s"' % (cs_name, stack_name))
    deployer = template_deployer.TemplateDeployer(change_set.stack)
    deployer.apply_change_set(change_set)
    change_set.stack.metadata['ChangeSetId'] = change_set.change_set_id
    return {}


def describe_change_set(req_params):
    stack_name = req_params.get('StackName')
    cs_name = req_params.get('ChangeSetName')
    change_set = find_change_set(cs_name, stack_name=stack_name)
    if not change_set:
        return error_response('Unable to find change set "%s" for stack "%s"' % (cs_name, stack_name))
    return change_set.metadata


def list_exports(req_params):
    state = RegionState.get()
    result = {'Exports': state.exports}
    return result


def validate_template(req_params):
    result = cloudformation_listener.validate_template(req_params)
    return result


def describe_stack_events(req_params):
    stack_name = req_params.get('StackName')
    state = RegionState.get()
    events = []
    for stack_id, stack in state.stacks.items():
        if stack_name in [None, stack.stack_name, stack.stack_id]:
            events.extend(stack.events)
    return {'StackEvents': {'member': events}}


def delete_change_set(req_params):
    stack_name = req_params.get('StackName')
    cs_name = req_params.get('ChangeSetName')
    change_set = find_change_set(cs_name, stack_name=stack_name)
    if not change_set:
        return error_response('Unable to find change set "%s" for stack "%s"' % (cs_name, stack_name))
    change_set.stack.change_sets = [cs for cs in change_set.stack.change_sets if cs.change_set_name != cs_name]
    return {}


# -----------------
# MAIN ENTRY POINT
# -----------------

@app.route('/', methods=['POST'])
def handle_request():
    data = request.get_data()
    req_params = parse_request_data(request.method, request.path, data)
    action = req_params.get('Action', '')

    func = ENDPOINTS.get(action)
    if not func:
        return '', 404
    result = func(req_params)

    result = _response(action, result)
    return result


ENDPOINTS = {
    'CreateChangeSet': create_change_set,
    'CreateStack': create_stack,
    'DeleteChangeSet': delete_change_set,
    'DeleteStack': delete_stack,
    'DescribeChangeSet': describe_change_set,
    'DescribeStackEvents': describe_stack_events,
    'DescribeStackResource': describe_stack_resource,
    'DescribeStackResources': describe_stack_resources,
    'DescribeStacks': describe_stacks,
    'ExecuteChangeSet': execute_change_set,
    'ListExports': list_exports,
    'ListStacks': list_stacks,
    'ListStackResources': list_stack_resources,
    'UpdateStack': update_stack,
    'ValidateTemplate': validate_template
}


# ---------------
# UTIL FUNCTIONS
# ---------------

def error_response(*args, **kwargs):
    kwargs['xmlns'] = kwargs.get('xmlns') or XMLNS_CF
    return flask_error_response_xml(*args, **kwargs)


def find_stack(stack_name):
    state = RegionState.get()
    return ([s for s in state.stacks.values() if stack_name == s.stack_name] or [None])[0]


def find_change_set(cs_name, stack_name=None):
    state = RegionState.get()
    stack = find_stack(stack_name)
    stacks = [stack] if stack else state.stacks.values()
    result = [cs for s in stacks for cs in s.change_sets if cs_name in [cs.change_set_id, cs.change_set_name]]
    return (result or [None])[0]


def _response(action, result):
    if isinstance(result, (dict, str)):
        result = requests_response_xml(action, result, xmlns=XMLNS_CF)
    if isinstance(result, requests.models.Response):
        result = requests_to_flask_response(result)
    return result


def serve(port, quiet=True):
    from localstack.services import generic_proxy  # moved here to fix circular import errors
    return generic_proxy.serve_flask_app(app=app, port=port, quiet=quiet)
