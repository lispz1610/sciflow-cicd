#!/usr/bin/env python3
"""Reconcile one explicitly selected GitHub Projects V2 project."""

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib import error, request


class GraphQLError(RuntimeError):
    """A GitHub GraphQL response cannot safely be used for reconciliation."""


COLORS = {'BLUE', 'GRAY', 'GREEN', 'ORANGE', 'PINK', 'PURPLE', 'RED', 'YELLOW'}
TYPES = {'TEXT', 'NUMBER', 'DATE', 'SINGLE_SELECT'}
LOGIN = re.compile(r'[A-Za-z0-9-]+\Z')


def validate_policy(policy):
    """Validate the whole desired state before any API mutation."""
    if not isinstance(policy, dict) or not {'version', 'owner_type', 'owner', 'title'} <= set(policy) or set(policy) - {'version', 'owner_type', 'owner', 'title', 'project_id', 'fields', 'items'}:
        raise ValueError('policy requires version, owner_type, owner, title and known optional keys')
    if type(policy['version']) is not int or policy['version'] != 1:
        raise ValueError('unsupported policy version')
    if policy['owner_type'] not in ('user', 'organization') or not isinstance(policy['owner'], str) or not LOGIN.fullmatch(policy['owner']):
        raise ValueError('owner_type must be user or organization and owner must be a login')
    if not isinstance(policy['title'], str) or not policy['title'].strip():
        raise ValueError('title must be nonempty')
    if 'project_id' in policy and (not isinstance(policy['project_id'], str) or not policy['project_id']):
        raise ValueError('project_id must be a nonempty GraphQL node ID')
    fields = policy.get('fields', [])
    items = policy.get('items', [])
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError('fields and items must be arrays')
    names = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {'name', 'type', 'options'} or not isinstance(field.get('name'), str) or not field['name'].strip() or not isinstance(field.get('type'), str) or field['type'] not in TYPES:
            raise ValueError('invalid field')
        if field['name'] in names:
            raise ValueError('duplicate field name')
        names.add(field['name'])
        if field['type'] == 'SINGLE_SELECT':
            options = field.get('options')
            if not isinstance(options, list) or not options:
                raise ValueError('single-select field requires options')
            option_names = set()
            for option in options:
                if not isinstance(option, dict) or set(option) != {'name', 'color', 'description'} or not isinstance(option['name'], str) or not option['name'].strip() or not isinstance(option['color'], str) or option['color'] not in COLORS or not isinstance(option['description'], str):
                    raise ValueError('invalid single-select option')
                if option['name'] in option_names:
                    raise ValueError('duplicate option name')
                option_names.add(option['name'])
        elif 'options' in field:
            raise ValueError('only single-select fields accept options')
    ids = set()
    field_by_name = {field['name']: field for field in fields}
    for item in items:
        if not isinstance(item, dict) or set(item) != {'content_id', 'type', 'values'} or not isinstance(item['content_id'], str) or not item['content_id'] or not isinstance(item['type'], str) or item['type'] not in ('Issue', 'PullRequest') or not isinstance(item['values'], dict):
            raise ValueError('invalid item')
        if item['content_id'] in ids:
            raise ValueError('duplicate item content_id')
        ids.add(item['content_id'])
        for name, value in item['values'].items():
            field = field_by_name.get(name)
            if field is None:
                raise ValueError(f'item value references undeclared field: {name}')
            kind = field['type']
            if kind in ('TEXT', 'SINGLE_SELECT') and (not isinstance(value, str) or (kind == 'SINGLE_SELECT' and value not in {o['name'] for o in field['options']})):
                raise ValueError(f'invalid {kind} value for {name}')
            if kind == 'NUMBER' and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f'invalid NUMBER value for {name}')
            if kind == 'DATE':
                try:
                    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
                except ValueError:
                    raise ValueError(f'invalid DATE value for {name}') from None
    return policy


QUERIES = {
    'UserProjects': '''query UserProjects($login:String!,$after:String){ owner:user(login:$login){ id login projectsV2(first:100,after:$after){nodes{id number title url} pageInfo{hasNextPage endCursor}}}}''',
    'OrganizationProjects': '''query OrganizationProjects($login:String!,$after:String){ owner:organization(login:$login){ id login projectsV2(first:100,after:$after){nodes{id number title url} pageInfo{hasNextPage endCursor}}}}''',
    'ProjectFields': '''query ProjectFields($projectId:ID!,$after:String){node(id:$projectId){... on ProjectV2{fields(first:100,after:$after){nodes{__typename ... on ProjectV2FieldCommon{id name dataType} ... on ProjectV2SingleSelectField{options{id name color description}}} pageInfo{hasNextPage endCursor}}}}}''',
    'ProjectItems': '''query ProjectItems($projectId:ID!,$after:String){node(id:$projectId){... on ProjectV2{items(first:100,after:$after,archivedStates:[ARCHIVED,NOT_ARCHIVED]){nodes{id content{__typename ... on Issue{id number repository{nameWithOwner}} ... on PullRequest{id}}} pageInfo{hasNextPage endCursor}}}}}''',
    'ItemValues': '''query ItemValues($itemId:ID!,$after:String){node(id:$itemId){... on ProjectV2Item{fieldValues(first:100,after:$after){nodes{__typename ... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{id}}} ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{id}}} ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{id}}} ... on ProjectV2ItemFieldSingleSelectValue{optionId field{... on ProjectV2FieldCommon{id}}}} pageInfo{hasNextPage endCursor}}}}}''',
    'ContentNode': '''query ContentNode($contentId:ID!){node(id:$contentId){__typename id}}''',
    'CreateProject': '''mutation CreateProject($ownerId:ID!,$title:String!){createProjectV2(input:{ownerId:$ownerId,title:$title}){projectV2{id}}}''',
    'CreateField': '''mutation CreateField($projectId:ID!,$name:String!,$dataType:ProjectV2CustomFieldType!,$options:[ProjectV2SingleSelectFieldOptionInput!]){createProjectV2Field(input:{projectId:$projectId,name:$name,dataType:$dataType,singleSelectOptions:$options}){projectV2Field{... on ProjectV2FieldCommon{id}}}}''',
    'UpdateField': '''mutation UpdateField($fieldId:ID!,$options:[ProjectV2SingleSelectFieldOptionInput!]){updateProjectV2Field(input:{fieldId:$fieldId,singleSelectOptions:$options}){projectV2Field{... on ProjectV2FieldCommon{id}}}}''',
    'AddItem': '''mutation AddItem($projectId:ID!,$contentId:ID!){addProjectV2ItemById(input:{projectId:$projectId,contentId:$contentId}){item{id}}}''',
    'UpdateValue': '''mutation UpdateValue($projectId:ID!,$itemId:ID!,$fieldId:ID!,$value:ProjectV2FieldValue!){updateProjectV2ItemFieldValue(input:{projectId:$projectId,itemId:$itemId,fieldId:$fieldId,value:$value}){projectV2Item{id}}}''',
}


class GitHubGraphQL:
    def __init__(self, token):
        if not token:
            raise ValueError('GITHUB_TOKEN is required')
        self.token = token

    def query(self, operation, variables):
        query = QUERIES[operation]
        payload = json.dumps({'query': query, 'variables': variables, 'operationName': operation}).encode()
        req = request.Request('https://api.github.com/graphql', data=payload, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json', 'User-Agent': 'sciflow-cicd'}, method='POST')
        try:
            with request.urlopen(req, timeout=30) as response:
                body = json.load(response)
        except error.HTTPError as exc:
            raise GraphQLError(f'GraphQL HTTP {exc.code}; check permissions and rate limits') from None
        except (error.URLError, TimeoutError, ValueError) as exc:
            raise GraphQLError(f'GraphQL transport failed: {type(exc).__name__}') from None
        if not isinstance(body, dict) or body.get('errors') or not isinstance(body.get('data'), dict):
            messages = [str(e.get('message', 'error')) for e in body.get('errors', []) if isinstance(e, dict)] if isinstance(body, dict) else []
            raise GraphQLError('GraphQL error: ' + '; '.join(messages or ['malformed response']))
        return body['data']


def _query(api, operation, variables):
    return api.query(operation, dict(variables))


def _pages(api, operation, variables, path):
    result = []
    after = None
    cursors = set()
    for _ in range(1000):
        data = _query(api, operation, dict(variables, after=after))
        cursor = data
        for key in path:
            if not isinstance(cursor, dict):
                raise GraphQLError(f'{operation}: malformed response or inaccessible node')
            cursor = cursor.get(key)
        if not isinstance(cursor, dict) or not isinstance(cursor.get('nodes'), list) or not isinstance(cursor.get('pageInfo'), dict):
            raise GraphQLError(f'{operation}: malformed connection')
        result.extend(cursor['nodes'])
        page = cursor['pageInfo']
        if page.get('hasNextPage') is False:
            return data, result
        next_cursor = page.get('endCursor')
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            raise GraphQLError(f'{operation}: invalid pagination cursor')
        cursors.add(next_cursor)
        after = next_cursor
    raise GraphQLError(f'{operation}: pagination limit exceeded')


def _unique(nodes, key, value, resource):
    matches = [node for node in nodes if isinstance(node, dict) and node.get(key) == value]
    if len(matches) > 1:
        raise GraphQLError(f'multiple {resource} match {value!r}')
    return matches[0] if matches else None


def _state(api, policy):
    operation = 'UserProjects' if policy['owner_type'] == 'user' else 'OrganizationProjects'
    data, projects = _pages(api, operation, {'login': policy['owner']}, ('owner', 'projectsV2'))
    owner = data.get('owner')
    if not isinstance(owner, dict) or not isinstance(owner.get('id'), str):
        raise GraphQLError('owner missing or inaccessible')
    if 'project_id' in policy:
        project = _unique(projects, 'id', policy['project_id'], 'projects')
        if project is None:
            raise GraphQLError('project_id is not accessible under the selected owner')
        if project.get('title') != policy['title']:
            raise GraphQLError('project_id title differs from policy title')
    else:
        project = _unique(projects, 'title', policy['title'], 'projects')
    fields = []
    items = []
    if project:
        if not isinstance(project.get('id'), str):
            raise GraphQLError('project has no stable node ID')
        _, fields = _pages(api, 'ProjectFields', {'projectId': project['id']}, ('node', 'fields'))
        _, items = _pages(api, 'ProjectItems', {'projectId': project['id']}, ('node', 'items'))
    for item in policy.get('items', []):
        content_id = item['content_id']
        content = _query(api, 'ContentNode', {'contentId': content_id}).get('node')
        if not isinstance(content, dict) or content.get('id') != content_id or content.get('__typename') != item['type']:
            raise GraphQLError(f'{content_id} is not an accessible {item["type"]}')
    return owner, project, fields, items


def _desired_option_inputs(desired, existing):
    options = existing.get('options')
    if not isinstance(options, list):
        raise GraphQLError('single-select field options unavailable')
    names = [o.get('name') for o in options if isinstance(o, dict)]
    if len(names) != len(set(names)):
        raise GraphQLError('duplicate existing option names')
    desired_by_name = {o['name']: o for o in desired}
    merged = []
    for option in options:
        if not isinstance(option, dict) or not all(isinstance(option.get(k), str) for k in ('id', 'name', 'color', 'description')):
            raise GraphQLError('malformed existing option')
        selected = desired_by_name.get(option['name'], option)
        merged.append({k: selected[k] for k in ('name', 'color', 'description')})
    for option in desired:
        if option['name'] not in names:
            merged.append({k: option[k] for k in ('name', 'color', 'description')})
    return merged


def _current_value(api, item_id, field_id):
    _, values = _pages(api, 'ItemValues', {'itemId': item_id}, ('node', 'fieldValues'))
    matches = [v for v in values if isinstance(v, dict) and isinstance(v.get('field'), dict) and v['field'].get('id') == field_id]
    if len(matches) > 1:
        raise GraphQLError('duplicate item field values')
    return matches[0] if matches else None


def _changes(api, policy, project, fields, items):
    changes = []
    if project is None:
        changes.append({'operation': 'create_project', 'title': policy['title']})
    field_map = {}
    for desired in policy.get('fields', []):
        field = _unique(fields, 'name', desired['name'], 'fields')
        field_map[desired['name']] = field
        if field is not None and (not isinstance(field.get('id'), str) or not field['id']):
            raise GraphQLError('existing field has no stable node ID')
        if field is None:
            changes.append({'operation': 'create_field', 'name': desired['name'], 'type': desired['type']})
        elif field.get('dataType') != desired['type']:
            raise GraphQLError(f'field type mismatch: {desired["name"]}')
        elif desired['type'] == 'SINGLE_SELECT':
            merged = _desired_option_inputs(desired['options'], field)
            if [(o['name'], o['color'], o['description']) for o in merged] != [(o['name'], o['color'], o['description']) for o in field['options']]:
                changes.append({'operation': 'update_field', 'name': desired['name']})
    for desired in policy.get('items', []):
        content_id = desired['content_id']
        matches = [i for i in items if isinstance(i, dict) and isinstance(i.get('content'), dict) and i['content'].get('id') == content_id]
        if len(matches) > 1:
            raise GraphQLError(f'multiple project items match {content_id!r}')
        item = matches[0] if matches else None
        if item is not None and (not isinstance(item.get('id'), str) or not item['id']):
            raise GraphQLError('existing item has no stable node ID')
        if item and item['content'].get('__typename') != desired['type']:
            raise GraphQLError('existing item has unexpected content type')
        if item is None:
            changes.append({'operation': 'add_item', 'content_id': content_id, 'type': desired['type']})
        for field_name, value in desired['values'].items():
            field = field_map[field_name]
            if item is None or field is None:
                changes.append({'operation': 'set_value', 'content_id': content_id, 'field': field_name, 'value': value})
                continue
            current = _current_value(api, item['id'], field['id'])
            kind = next(f['type'] for f in policy['fields'] if f['name'] == field_name)
            if kind == 'SINGLE_SELECT':
                option = _unique(field['options'], 'name', value, 'options')
                if option is None:
                    changes.append({'operation': 'set_value', 'content_id': content_id, 'field': field_name, 'value': value})
                    continue
                observed = current.get('optionId') if current else None
                target = option['id']
            else:
                key = kind.lower()
                observed = current.get(key) if current else None
                target = value
            if observed != target:
                changes.append({'operation': 'set_value', 'content_id': content_id, 'field': field_name, 'value': value})
    return changes


def _mutation(api, operation, variables, response_path):
    node = _query(api, operation, variables)
    for key in response_path:
        if not isinstance(node, dict):
            raise GraphQLError(f'{operation}: malformed mutation response')
        node = node.get(key)
    if not isinstance(node, str) or not node:
        raise GraphQLError(f'{operation}: missing result ID')
    return node


def reconcile(api, policy, apply=False):
    validate_policy(policy)
    owner, project, fields, items = _state(api, policy)
    changes = _changes(api, policy, project, fields, items)
    result = {'owner_type': policy['owner_type'], 'owner': policy['owner'], 'title': policy['title'],
              'project_id': project['id'] if project else None, 'changes': changes,
              'status': 'planned' if not apply else 'verified'}
    if not apply or not changes:
        return result
    if project is None:
        result['project_id'] = _mutation(api, 'CreateProject', {'ownerId': owner['id'], 'title': policy['title']}, ('createProjectV2', 'projectV2', 'id'))
    project_id = result['project_id']
    for desired in policy.get('fields', []):
        field = _unique(fields, 'name', desired['name'], 'fields')
        if field is None:
            variables = {'projectId': project_id, 'name': desired['name'], 'dataType': desired['type'],
                         'options': desired.get('options') if desired['type'] == 'SINGLE_SELECT' else None}
            _mutation(api, 'CreateField', variables, ('createProjectV2Field', 'projectV2Field', 'id'))
        elif desired['type'] == 'SINGLE_SELECT':
            options = _desired_option_inputs(desired['options'], field)
            if [(o['name'], o['color'], o['description']) for o in options] != [(o['name'], o['color'], o['description']) for o in field['options']]:
                _mutation(api, 'UpdateField', {'fieldId': field['id'], 'options': options}, ('updateProjectV2Field', 'projectV2Field', 'id'))
    _, fields = _pages(api, 'ProjectFields', {'projectId': project_id}, ('node', 'fields'))
    added_items = False
    for desired in policy.get('items', []):
        item = next((i for i in items if isinstance(i, dict) and isinstance(i.get('content'), dict) and i['content'].get('id') == desired['content_id']), None)
        if item is None:
            _mutation(api, 'AddItem', {'projectId': project_id, 'contentId': desired['content_id']}, ('addProjectV2ItemById', 'item', 'id'))
            added_items = True
    if added_items:
        expected_ids = {desired['content_id'] for desired in policy.get('items', [])}
        for attempt in range(6):
            _, items = _pages(api, 'ProjectItems', {'projectId': project_id}, ('node', 'items'))
            visible_ids = {item.get('content', {}).get('id') for item in items
                           if isinstance(item, dict) and isinstance(item.get('content'), dict)}
            if expected_ids <= visible_ids:
                break
            if attempt < 5:
                time.sleep(1)
        else:
            raise GraphQLError('added item is not visible for verification')
    for desired in policy.get('items', []):
        item = next((i for i in items if isinstance(i, dict) and isinstance(i.get('content'), dict) and i['content'].get('id') == desired['content_id']), None)
        if item is None:
            raise GraphQLError('added item is not visible for verification')
        for name, value in desired['values'].items():
            field = _unique(fields, 'name', name, 'fields')
            if field is None:
                raise GraphQLError('created field is not visible for verification')
            kind = next(f['type'] for f in policy['fields'] if f['name'] == name)
            if kind == 'SINGLE_SELECT':
                option = _unique(field.get('options', []), 'name', value, 'options')
                if option is None:
                    raise GraphQLError('desired option is not visible for verification')
                target = option['id']
                key = 'optionId'
                payload = {'singleSelectOptionId': target}
            else:
                key = kind.lower()
                target = value
                payload = {key: value}
            current = _current_value(api, item['id'], field['id'])
            if current is None or current.get(key) != target:
                _mutation(api, 'UpdateValue', {'projectId': project_id, 'itemId': item['id'], 'fieldId': field['id'], 'value': payload}, ('updateProjectV2ItemFieldValue', 'projectV2Item', 'id'))
    _, final_project, final_fields, final_items = _state(api, dict(policy, project_id=project_id))
    remaining = _changes(api, policy, final_project, final_fields, final_items)
    if remaining:
        raise GraphQLError(f'apply could not verify convergence: {remaining}')
    result['url'] = final_project.get('url')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True, type=Path, help='Path to owner-scoped Projects V2 JSON policy')
    parser.add_argument('--apply', action='store_true', help='Write the selected changes')
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text(encoding='utf-8'))
        validate_policy(policy)
        api = GitHubGraphQL(os.environ.get('GITHUB_TOKEN'))
        result = reconcile(api, policy, apply=args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (OSError, ValueError, GraphQLError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
