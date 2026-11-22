import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import setup_github_projects as projects
from setup_github_projects import GraphQLError, reconcile, validate_policy


class FakeGraphQL:
    def __init__(self, owner_type='User'):
        self.owner_type = owner_type
        self.projects = []
        self.contents = {'I1': 'Issue', 'R1': 'PullRequest'}
        self.calls = []
        self.fail = None
        self.hide_item_reads = 0

    def query(self, operation, variables):
        self.calls.append((operation, copy.deepcopy(variables)))
        if self.fail == operation:
            raise GraphQLError('permission denied or rate limit exceeded')
        if operation in ('UserProjects', 'OrganizationProjects'):
            all_projects = self.projects
            offset = int(variables.get('after') or 0)
            nodes = [{'id': p['id'], 'title': p['title'], 'url': p['url']} for p in all_projects[offset:offset+1]]
            end = offset + len(nodes)
            return {'owner': {'id': 'OWNER1', 'projectsV2': {'nodes': nodes, 'pageInfo': {'hasNextPage': end < len(all_projects), 'endCursor': str(end)}}}}
        if operation == 'ProjectFields':
            p = self._project(variables['projectId'])
            return {'node': {'fields': self._page(p['fields'], variables.get('after'))}}
        if operation == 'ProjectItems':
            p = self._project(variables['projectId'])
            if p['items'] and self.hide_item_reads:
                self.hide_item_reads -= 1
                return {'node': {'items': self._page([], variables.get('after'))}}
            return {'node': {'items': self._page(p['items'], variables.get('after'))}}
        if operation == 'ItemValues':
            p = next(p for p in self.projects if any(i['id'] == variables['itemId'] for i in p['items']))
            item = next(i for i in p['items'] if i['id'] == variables['itemId'])
            return {'node': {'fieldValues': self._page(item['values'], variables.get('after'))}}
        if operation == 'ContentNode':
            kind = self.contents.get(variables['contentId'])
            return {'node': {'id': variables['contentId'], '__typename': kind} if kind else None}
        if operation == 'CreateProject':
            p = {'id': f'P{len(self.projects)+1}', 'title': variables['title'], 'url': 'https://github.com/users/u/projects/1', 'fields': [], 'items': []}
            self.projects.append(p)
            return {'createProjectV2': {'projectV2': {'id': p['id']}}}
        if operation == 'CreateField':
            p = self._project(variables['projectId'])
            f = {'id': f'F{len(p["fields"])+1}', 'name': variables['name'], 'dataType': variables['dataType'], '__typename': 'ProjectV2SingleSelectField' if variables['dataType'] == 'SINGLE_SELECT' else 'ProjectV2Field'}
            if variables['dataType'] == 'SINGLE_SELECT':
                f['options'] = [dict(o, id=f'O{n+1}') for n, o in enumerate(variables['options'])]
            p['fields'].append(f)
            return {'createProjectV2Field': {'projectV2Field': {'id': f['id']}}}
        if operation == 'UpdateField':
            f = next(x for p in self.projects for x in p['fields'] if x['id'] == variables['fieldId'])
            existing_ids = {o['name']: o['id'] for o in f['options']}
            f['options'] = [dict(o, id=existing_ids.get(o['name']) or f'O{n+1}') for n, o in enumerate(variables['options'])]
            return {'updateProjectV2Field': {'projectV2Field': {'id': f['id']}}}
        if operation == 'AddItem':
            p = self._project(variables['projectId'])
            i = {'id': f'IT{len(p["items"])+1}', 'content': {'id': variables['contentId'], '__typename': self.contents[variables['contentId']]}, 'values': []}
            p['items'].append(i)
            return {'addProjectV2ItemById': {'item': {'id': i['id']}}}
        if operation == 'UpdateValue':
            p = self._project(variables['projectId'])
            i = next(x for x in p['items'] if x['id'] == variables['itemId'])
            v = next((x for x in i['values'] if x['field']['id'] == variables['fieldId']), None)
            if v is None:
                v = {'field': {'id': variables['fieldId']}}
                i['values'].append(v)
            v.update({'optionId': variables['value']['singleSelectOptionId']} if 'singleSelectOptionId' in variables['value'] else variables['value'])
            return {'updateProjectV2ItemFieldValue': {'projectV2Item': {'id': i['id']}}}
        raise AssertionError(operation)

    def _project(self, project_id):
        return next(p for p in self.projects if p['id'] == project_id)

    @staticmethod
    def _page(values, after):
        offset = int(after or 0)
        nodes = copy.deepcopy(values[offset:offset+1])
        end = offset + len(nodes)
        return {'nodes': nodes, 'pageInfo': {'hasNextPage': end < len(values), 'endCursor': str(end)}}


POLICY = {'version': 1, 'owner_type': 'user', 'owner': 'u', 'title': 'Roadmap',
          'fields': [{'name': 'State', 'type': 'SINGLE_SELECT', 'options': [
              {'name': 'Todo', 'color': 'BLUE', 'description': ''}]}],
          'items': [{'content_id': 'I1', 'type': 'Issue', 'values': {'State': 'Todo'}},
                    {'content_id': 'R1', 'type': 'PullRequest', 'values': {'State': 'Todo'}}]}


class ProjectSetupTests(unittest.TestCase):
    def test_direct_cli_dry_run_apply_and_repeat(self):
        api = FakeGraphQL()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "project.json"
            source.write_text(json.dumps(POLICY), encoding="utf-8")
            with mock.patch.object(projects, "GitHubGraphQL", return_value=api), \
                 mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(projects.main(["--policy", str(source)]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "planned")
                self.assertEqual(api.projects, [])
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(projects.main(["--policy", str(source), "--apply"]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "verified")
                self.assertEqual(len(api.projects), 1)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(projects.main(["--policy", str(source), "--apply"]), 0)
                self.assertEqual(json.loads(output.getvalue())["changes"], [])

    def test_dry_run_apply_and_second_apply_noop(self):
        api = FakeGraphQL()
        plan = reconcile(api, POLICY, apply=False)
        self.assertEqual(['create_project', 'create_field', 'add_item', 'set_value', 'add_item', 'set_value'], [x['operation'] for x in plan['changes']])
        self.assertEqual([], api.projects)
        applied = reconcile(api, POLICY, apply=True)
        self.assertEqual('P1', applied['project_id'])
        self.assertEqual([], reconcile(api, dict(POLICY, project_id='P1'), apply=True)['changes'])
        self.assertEqual(1, len(api.projects))
        self.assertEqual(2, len(api.projects[0]['items']))
        mutations = [x[0] for x in api.calls if x[0] in ('AddItem', 'UpdateValue')]
        self.assertLess(mutations.index('AddItem'), mutations.index('UpdateValue'))

    def test_organization_and_unrelated_resources(self):
        api = FakeGraphQL('Organization')
        api.projects.append({'id': 'P0', 'title': 'Other', 'url': 'https://github.com/orgs/o/projects/1', 'fields': [], 'items': []})
        before = copy.deepcopy(api.projects[0])
        result = reconcile(api, dict(POLICY, owner_type='organization', owner='o'), apply=True)
        self.assertEqual('P2', result['project_id'])
        self.assertEqual(before, api.projects[0])

    def test_duplicate_and_mismatched_identity_fail_closed(self):
        api = FakeGraphQL()
        for n in range(2):
            api.projects.append({'id': f'P{n}', 'title': 'Roadmap', 'url': 'u', 'fields': [], 'items': []})
        with self.assertRaisesRegex(GraphQLError, 'multiple projects'):
            reconcile(api, POLICY)
        with self.assertRaisesRegex(GraphQLError, 'title'):
            reconcile(api, dict(POLICY, project_id='P0', title='Renamed'))

    def test_missing_content_permission_and_rate_limit_have_no_writes(self):
        api = FakeGraphQL()
        api.contents.pop('I1')
        with self.assertRaisesRegex(GraphQLError, 'not an accessible'):
            reconcile(api, POLICY, apply=True)
        self.assertEqual([], api.projects)
        api.fail = 'UserProjects'
        with self.assertRaises(GraphQLError):
            reconcile(api, POLICY, apply=True)
        self.assertEqual([], api.projects)

    def test_options_update_preserves_existing_option_ids_and_unrelated_options(self):
        api = FakeGraphQL()
        p = {'id': 'P1', 'title': 'Roadmap', 'url': 'u', 'items': [], 'fields': [
            {'id': 'F1', 'name': 'State', 'dataType': 'SINGLE_SELECT', '__typename': 'ProjectV2SingleSelectField', 'options': [
                {'id': 'O1', 'name': 'Todo', 'color': 'RED', 'description': ''},
                {'id': 'O2', 'name': 'Unrelated', 'color': 'GREEN', 'description': ''}]}]}
        api.projects.append(p)
        reconcile(api, POLICY, apply=True)
        self.assertEqual(['O1', 'O2'], [o['id'] for o in p['fields'][0]['options']])
        self.assertEqual('BLUE', p['fields'][0]['options'][0]['color'])
        self.assertEqual('Unrelated', p['fields'][0]['options'][1]['name'])

    def test_duplicate_field_or_item_stops_before_any_mutation(self):
        api = FakeGraphQL()
        p = {'id': 'P1', 'title': 'Roadmap', 'url': 'u', 'fields': [
            {'id': 'F1', 'name': 'State', 'dataType': 'SINGLE_SELECT', 'options': []},
            {'id': 'F2', 'name': 'State', 'dataType': 'SINGLE_SELECT', 'options': []}], 'items': []}
        api.projects.append(p)
        with self.assertRaisesRegex(GraphQLError, 'multiple fields'):
            reconcile(api, POLICY, apply=True)
        self.assertFalse(any(name in ('CreateField', 'AddItem', 'UpdateValue') for name, _ in api.calls))
        p['fields'] = []
        p['items'] = [
            {'id': 'IT1', 'content': {'id': 'I1', '__typename': 'Issue'}, 'values': []},
            {'id': 'IT2', 'content': {'id': 'I1', '__typename': 'Issue'}, 'values': []}]
        api.calls.clear()
        with self.assertRaisesRegex(GraphQLError, 'multiple project items'):
            reconcile(api, POLICY, apply=True)
        self.assertFalse(any(name in ('CreateField', 'AddItem', 'UpdateValue') for name, _ in api.calls))

    def test_failed_mutation_can_be_retried(self):
        api = FakeGraphQL()
        api.fail = 'AddItem'
        with self.assertRaises(GraphQLError):
            reconcile(api, POLICY, apply=True)
        self.assertEqual(1, len(api.projects))
        self.assertEqual(1, len(api.projects[0]['fields']))
        api.fail = None
        reconcile(api, POLICY, apply=True)
        self.assertEqual([], reconcile(api, POLICY, apply=True)['changes'])

    def test_new_item_eventually_becomes_visible_before_field_update(self):
        api = FakeGraphQL()
        api.hide_item_reads = 1
        with mock.patch('setup_github_projects.time.sleep', return_value=None):
            result = reconcile(api, POLICY, apply=True)
        self.assertEqual('verified', result['status'])
        self.assertEqual([], reconcile(api, POLICY)['changes'])

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            validate_policy(dict(POLICY, fields=[{'name': 'State', 'type': 'SINGLE_SELECT', 'options': []}]))
        with self.assertRaises(ValueError):
            validate_policy(dict(POLICY, items=[POLICY['items'][0], POLICY['items'][0]]))


if __name__ == '__main__':
    unittest.main()
