"""Exercise Hexium publication without credentials or external writes."""
import hashlib
from http.client import HTTPException, IncompleteRead
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
import publish_hexium as hexium
import test_thunderstore as fixtures
from release_assets import CredentialRedirect


class FakeMetadata:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    def update(self, namespace, name):
        self.calls.append((namespace, name))
        self.fixture.http.responses[self.fixture.package_url] = self.fixture.public()
        return {'namespace': namespace, 'name': name, 'install_location': 'both',
                'categories': list(hexium.CATEGORIES)}


class HexiumTests(unittest.TestCase):
    # Reuse the disposable tagged GitHub release fixture, retaining the existing
    # Thunderstore suite independently. No inherited publisher tests are rerun.
    git = fixtures.ThunderstoreTests.git
    write_archive = fixtures.ThunderstoreTests.write_archive

    def setUp(self):
        fixtures.ThunderstoreTests.setUp(self)
        self.version_url = f'{hexium.HEXIUM}/api/experimental/package/randenko/GuildChest/1.2.3/'
        self.package_url = f'{hexium.HEXIUM}/api/experimental/package/randenko/GuildChest/'
        self.download_url = 'https://cdn.hexium.gg/upload/123/1.2.3.zip'
        self.listing = f'{hexium.HEXIUM}/mods/randenko/GuildChest'
        self.categories_url = f'{hexium.HEXIUM}/api/experimental/community/valheim/category/'
        self.http.responses.update({
            self.version_url: None,
            self.package_url: self.public(complete=False),
            self.categories_url: {'results': [{'slug': slug} for slug in hexium.CATEGORIES], 'next': None},
            self.listing + '/versions': self.scan(),
        })
        for dependency in self.manifest['dependencies']:
            namespace, name, version = dependency.split('-')
            self.http.responses[f'{hexium.HEXIUM}/api/experimental/package/{namespace}/{name}/{version}/'] = {
                'namespace': namespace, 'name': name, 'version_number': version,
                'full_name': dependency, 'is_active': True}
        self.metadata = FakeMetadata(self)
        self.uploads = []
        self.sleeps = []

    def existing(self):
        return {'namespace': 'randenko', 'name': 'GuildChest', 'version_number': '1.2.3',
                'is_active': True, 'download_url': self.download_url}

    def public(self, complete=True):
        return {'namespace': 'randenko', 'name': 'GuildChest', 'community_listings': [
            {'community': 'valheim', 'review_status': 'approved',
             'categories': list(hexium.CATEGORIES) + ['Client & Server'] if complete else ['Storage']}]}

    def scan(self, version='1.2.3', status='clean'):
        return (f'<div class="version-entry surface-card" id="v-{version}"><div>'
                f'<a class="status-badge status-badge--vt-{status}">Scan</a></div></div>').encode()

    def record(self, args, **kwargs):
        self.uploads.append({'args': args, 'kwargs': kwargs,
                             'archive': Path(args[args.index('--file') + 1]).read_bytes(),
                             'config': tomllib.loads(Path(args[args.index('--config-path') + 1]).read_text())})
        return subprocess.CompletedProcess(args, 0)

    def run_publish(self, **kwargs):
        defaults = dict(repository=self.root, namespace='randenko', token='hexium-fixture',
                        http=self.http, metadata=self.metadata, enabled='true', run=self.record,
                        sleep=self.sleeps.append, now=lambda: 0)
        return hexium.publish(kwargs.pop('tag', 'v1.2.3'), **{**defaults, **kwargs})

    def assert_rejected(self, message, **kwargs):
        with self.assertRaisesRegex((ValueError, zipfile.BadZipFile), message):
            self.run_publish(preflight=False, **kwargs)
        self.assertFalse(self.uploads)
        self.assertFalse(self.metadata.calls)

    def ready(self, contents=None, complete=True):
        self.http.responses[self.version_url] = self.existing()
        self.http.responses[self.download_url] = contents or self.contents
        self.http.responses[self.package_url] = self.public(complete)

    def archive(self, files=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in (files or self.files).items():
                archive.writestr(name, value)
        return stream.getvalue()

    def test_default_preflight_is_get_only_with_disabled_switch(self):
        message = self.run_publish(enabled='false')
        self.assertIn('Preflight passed', message)
        self.assertIn(self.listing, message)
        self.assertIn(hashlib.sha256(self.contents).hexdigest(), message)
        self.assertIn('Content verification: pending', message)
        self.assertFalse(self.uploads)
        self.assertFalse(self.metadata.calls)
        self.assertTrue(all('thunderstore.io' not in url for url in self.http.calls))

    def test_upload_guard_precedes_all_remote_calls(self):
        for enabled in ('', 'false', 'True', '1', True):
            with self.subTest(enabled=enabled):
                self.assert_rejected('HEXIUM_PUBLISH_ENABLED must be true', enabled=enabled)
                self.assertFalse(self.http.calls)
        with self.assertRaisesRegex(ValueError, 'HEXIUM_PUBLISH_ENABLED must be true'):
            hexium.publish('v1.2.3', preflight=False)

    def test_invalid_tag_and_missing_configuration(self):
        for tag in ('v1.2.3-beta', 'v01.2.3', 'v1.2.3\n', '--help'):
            self.assert_rejected('Only stable', tag=tag)
        for namespace in ('', 'unsafe/namespace', 'randenko\n'):
            self.assert_rejected('HEXIUM_NAMESPACE', namespace=namespace)
        self.assert_rejected('HEXIUM_API_TOKEN', token=' ')
        self.assertFalse(self.http.calls)

    def test_release_must_be_published_stable_and_on_main(self):
        for field, value in (('draft', True), ('prerelease', True), ('published_at', None), ('tag_name', 'v9.9.9')):
            self.http.responses[self.release_url] = {**self.release, field: value}
            self.assert_rejected('stable and published')
        self.http.responses[self.release_url] = self.release
        self.git('checkout', '-b', 'unmerged')
        self.git('commit', '--allow-empty', '-m', 'Unmerged')
        self.git('tag', '-f', 'v1.2.3')
        self.assert_rejected('must belong to origin/main')

    def test_shared_validator_rejects_checksum_archive_and_tag_metadata_changes(self):
        self.http.responses[self.sha_url] = b'0' * 64 + b'  GuildChest-1.2.3.zip\n'
        self.assert_rejected('checksum mismatch')
        self.files['manifest.json'] = json.dumps({**self.manifest, 'description': 'Changed'}).encode()
        self.write_archive()
        self.assert_rejected('does not match the tagged manifest')
        self.files.pop('LICENSE')
        self.write_archive()
        self.assert_rejected('structure')

    def test_upload_uses_original_zip_tag_metadata_and_isolated_environment(self):
        (self.root / 'manifest.json').write_text('{}')
        self.http.responses[self.version_url] = [None, None, self.existing()]
        self.http.responses[self.download_url] = self.contents
        credentials = {'GH_TOKEN': 'github-fixture', 'GITHUB_TOKEN': 'github-fixture',
                       'NEXUSMODS_API_KEY': 'nexus-fixture', 'THUNDERSTORE_API_TOKEN': 'thunder-fixture',
                       'HEXIUM_API_TOKEN': 'hexium-fixture', 'TCLI_REPOSITORY': 'https://wrong.invalid',
                       'TCLI_AUTH_TOKEN': 'wrong-fixture'}
        with patch.dict(os.environ, credentials):
            message = self.run_publish(preflight=False)
        self.assertIn('Published and verified', message)
        self.assertIn('Downloaded SHA-256: ' + hashlib.sha256(self.contents).hexdigest(), message)
        self.assertIn('Content verification: passed', message)
        self.assertIn('Virus scan: successful', message)
        self.assertEqual(len(self.uploads), 1)
        upload = self.uploads[0]
        self.assertEqual(upload['archive'], self.contents)
        self.assertEqual(upload['args'][0:2], ['tcli', 'publish'])
        self.assertNotIn('hexium-fixture', upload['args'])
        self.assertEqual(upload['config']['publish']['repository'], hexium.HEXIUM)
        self.assertEqual(upload['config']['publish']['categories']['valheim'], list(hexium.CATEGORIES))
        self.assertEqual(upload['config']['package']['namespace'], 'randenko')
        self.assertEqual(upload['config']['package']['versionNumber'], '1.2.3')
        self.assertEqual(len(upload['config']['package']['dependencies']), 2)
        environment = upload['kwargs']['env']
        self.assertEqual(environment['TCLI_AUTH_TOKEN'], 'hexium-fixture')
        for key in credentials.keys() - {'TCLI_AUTH_TOKEN'}:
            self.assertNotIn(key, environment)
        self.assertEqual(self.metadata.calls, [('randenko', 'GuildChest')])

    def test_manifest_formatting_and_only_bepinex_removal_are_accepted(self):
        for dependencies in (self.manifest['dependencies'], [d for d in self.manifest['dependencies']
                                                            if not d.startswith(hexium.BEPINEX)]):
            files = {**self.files, 'manifest.json': json.dumps(
                {**self.manifest, 'dependencies': dependencies}, indent=4, sort_keys=True).encode()}
            downloaded = self.archive(files)
            self.assertNotEqual(downloaded, self.contents)
            self.ready(downloaded)
            message = self.run_publish(preflight=False)
            self.assertIn('Already published', message)
            self.assertIn(hashlib.sha256(downloaded).hexdigest(), message)
        self.assertFalse(self.uploads)
        self.assertFalse(self.metadata.calls)

    def test_changed_dll_readme_changelog_icon_and_other_files_are_rejected(self):
        for name in self.files.keys() - {'manifest.json', 'icon.png'}:
            with self.subTest(name=name):
                self.ready(self.archive({**self.files, name: self.files[name] + b'changed'}))
                self.assert_rejected('different contents')
        self.ready(self.archive({**self.files, 'icon.png': b'changed'}))
        self.assert_rejected('Icon')

    def test_added_missing_duplicate_and_symlink_entries_are_rejected(self):
        for files in ({**self.files, 'extra.txt': b'Unexpected'},
                      {name: value for name, value in self.files.items() if name != 'LICENSE'}):
            self.ready(self.archive(files))
            self.assert_rejected('structure')
        stream = io.BytesIO(self.contents)
        with zipfile.ZipFile(stream, 'a') as archive:
            with self.assertWarns(UserWarning):
                archive.writestr('LICENSE', b'duplicate')
        self.ready(stream.getvalue())
        self.assert_rejected('structure')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, contents in self.files.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o120777 if name == 'LICENSE' else 0o100644) << 16
                archive.writestr(info, contents)
        self.ready(stream.getvalue())
        self.assert_rejected('symlink')

    def test_manifest_changes_other_than_bepinex_removal_are_rejected(self):
        for changed in ({'dependencies': []}, {'dependencies': list(reversed(self.manifest['dependencies']))},
                        {'description': 'Changed'}, {'extra': 'value'}, {'name': 'Other'},
                        {'version_number': '1.2.4'}, {'website_url': 'https://wrong.invalid'},
                        {'dependencies': ['ValheimModding-Jotunn-2.30.1']}):
            with self.subTest(changed=changed):
                self.ready(self.archive({**self.files, 'manifest.json': json.dumps({**self.manifest, **changed}).encode()}))
                self.assert_rejected('unsupported metadata changes')
        manifest = json.dumps(self.manifest).removesuffix('}') + ', "name": "GuildChest"}'
        self.ready(self.archive({**self.files, 'manifest.json': manifest.encode()}))
        self.assert_rejected('duplicate keys')

    def test_missing_inactive_or_wrong_dependencies_and_missing_categories_fail(self):
        url = f'{hexium.HEXIUM}/api/experimental/package/ValheimModding/Jotunn/2.30.2/'
        original = self.http.responses[url]
        for value in (None, {**original, 'is_active': False}, {**original, 'version_number': '2.30.1'}):
            self.http.responses[url] = value
            self.assert_rejected('dependency is unavailable')
        self.http.responses[url] = original
        self.http.responses[self.categories_url]['results'].pop()
        self.assert_rejected('Unavailable Hexium categories')

    def test_category_pagination_uses_hexium_next_and_rejects_cycles_or_external_targets(self):
        second = self.categories_url + '?page=2'
        self.http.responses[self.categories_url] = {'results': [{'slug': 'Building'}], 'next': '?page=2'}
        self.http.responses[second] = {'results': [{'slug': slug} for slug in hexium.CATEGORIES[1:]], 'next': None}
        self.assertIn('Preflight passed', self.run_publish())
        self.assertIn(second, self.http.calls)
        for next_url in (self.categories_url, 'https://evil.invalid/categories/',
                         'https://valheim.hexium.gg.evil.invalid/categories/', '/other-path/'):
            self.http.responses[second]['next'] = next_url
            self.assert_rejected('Invalid Hexium category pagination')

    def test_preflight_checks_existing_archive_but_never_finishes_metadata(self):
        self.ready(complete=False)
        self.assertIn('Content verification: passed', self.run_publish())
        self.assertFalse(self.metadata.calls)
        self.assertFalse(self.uploads)

    def test_retry_finishes_existing_metadata_without_upload(self):
        self.ready(complete=False)
        self.assertIn('Already published', self.run_publish(preflight=False))
        self.assertEqual(len(self.metadata.calls), 1)
        self.assertFalse(self.uploads)
        self.run_publish(preflight=False)
        self.assertEqual(len(self.metadata.calls), 1)

    def test_lost_metadata_response_is_reconciled_on_retry(self):
        self.ready(complete=False)
        update = self.metadata.update

        def lost_response(namespace, name):
            update(namespace, name)
            raise ValueError('Metadata response was lost')

        with patch.object(self.metadata, 'update', side_effect=lost_response):
            with self.assertRaisesRegex(ValueError, 'response was lost'):
                self.run_publish(preflight=False)
        self.assertIn('Already published', self.run_publish(preflight=False))
        self.assertEqual(len(self.metadata.calls), 1)
        self.assertFalse(self.uploads)

    def test_conflicting_version_identity_never_allows_upload_or_metadata_writes(self):
        for change in ({'namespace': 'wrong'}, {'version_number': '1.2.4'}, {'name': 'Other'}):
            self.http.responses[self.version_url] = {**self.existing(), **change}
            self.assert_rejected('different package version')

    def test_known_version_without_download_never_allows_upload(self):
        self.http.responses[self.version_url] = {**self.existing(), 'download_url': ''}
        self.assert_rejected('publication incomplete')

    def test_metadata_response_and_public_listing_must_agree(self):
        self.ready(complete=False)
        with patch.object(self.metadata, 'update', return_value={
                'namespace': 'wrong', 'name': 'GuildChest', 'install_location': 'both',
                'categories': list(hexium.CATEGORIES)}):
            with self.assertRaisesRegex(ValueError, 'metadata response'):
                self.run_publish(preflight=False)
        with patch.object(self.metadata, 'update', return_value={
                'namespace': 'randenko', 'name': 'GuildChest', 'install_location': 'both',
                'categories': list(hexium.CATEGORIES)}):
            with self.assertRaisesRegex(hexium.Pending, 'listing metadata is not complete'):
                self.run_publish(preflight=False)
        self.assertFalse(self.uploads)

    def test_cli_error_timeout_and_oserror_reconcile_accepted_version(self):
        for result in (subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired('tcli', 3600), OSError('lost')):
            self.http.responses[self.version_url] = [None, self.existing()]
            self.http.responses[self.download_url] = self.contents
            with patch.object(self, 'record', side_effect=result if isinstance(result, Exception) else None,
                              return_value=result):
                self.assertIn('Published and verified', self.run_publish(preflight=False))

    def test_delayed_publication_and_download_visibility_are_polled(self):
        self.http.responses[self.version_url] = [None, None, self.existing()]
        self.http.responses[self.download_url] = [None, self.contents]
        self.http.responses[self.package_url] = [None, self.public()]
        self.http.responses[self.listing + '/versions'] = [self.scan(status='pending'), self.scan()]
        message = self.run_publish(preflight=False)
        self.assertIn('Published and verified', message)
        self.assertIn('Virus scan: pending', message)
        self.assertEqual(len(self.uploads), 1)
        self.assertGreaterEqual(len(self.sleeps), 3)
        self.assertEqual(self.http.calls.count(self.listing + '/versions'), 1)

    def test_known_inactive_version_is_polled_without_duplicate_upload(self):
        self.http.responses[self.version_url] = [{**self.existing(), 'is_active': False}, self.existing()]
        self.http.responses[self.download_url] = self.contents
        self.assertIn('Already published', self.run_publish(preflight=False))
        self.assertFalse(self.uploads)

    def test_unconfirmed_publication_requires_retry_but_pending_scan_does_not(self):
        with self.assertRaisesRegex(hexium.Pending, 'publication incomplete.*Retry this release tag'):
            self.run_publish(preflight=False)
        self.assertEqual(sum(self.sleeps), 300)
        self.assertEqual(len(self.uploads), 1)
        self.ready()
        self.http.responses[self.listing + '/versions'] = self.scan(status='pending')
        self.sleeps.clear()
        message = self.run_publish(preflight=False)
        self.assertIn('Already published; content and metadata verified', message)
        self.assertIn('Virus scan: pending', message)
        self.assertFalse(self.sleeps)
        self.http.responses[self.listing + '/versions'] = self.scan()
        self.assertIn('Virus scan: successful', self.run_publish(preflight=False))
        self.assertEqual(len(self.uploads), 1)

    def test_successful_scan_for_another_version_does_not_mark_requested_scan_successful(self):
        self.ready()
        self.http.responses[self.listing + '/versions'] = self.scan(version='1.2.4') + self.scan(status='pending')
        message = self.run_publish(preflight=False)
        self.assertIn('Already published', message)
        self.assertIn('Virus scan: pending', message)
        self.assertFalse(self.sleeps)
        self.assertFalse(self.uploads)

    def test_missing_or_pending_scan_is_reported_without_waiting(self):
        self.ready()
        for page in (None, b'<html>Virus scan: &mdash;</html>', self.scan(status='pending'),
                     self.scan(version='1.2.4'), b'\xff'):
            with self.subTest(page=page):
                self.http.responses[self.listing + '/versions'] = page
                self.http.calls.clear()
                message = self.run_publish(preflight=False)
                self.assertIn('Already published; content and metadata verified', message)
                self.assertIn('Content verification: passed', message)
                self.assertIn('Virus scan: pending', message)
                self.assertIn(self.listing + '/versions#v-1.2.3', message)
                self.assertEqual(self.http.calls.count(self.listing + '/versions'), 1)
        self.assertFalse(self.sleeps)
        self.assertFalse(self.uploads)
        self.assertFalse(self.metadata.calls)

    def test_new_upload_succeeds_with_pending_scan_without_waiting(self):
        self.http.responses[self.version_url] = [None, self.existing()]
        self.http.responses[self.download_url] = self.contents
        self.http.responses[self.listing + '/versions'] = self.scan(status='pending')
        message = self.run_publish(preflight=False)
        self.assertIn('Published and verified', message)
        self.assertIn('Content verification: passed', message)
        self.assertIn('Virus scan: pending', message)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len(self.metadata.calls), 1)
        self.assertFalse(self.sleeps)

    def test_scan_request_errors_do_not_fail_verified_publication(self):
        self.ready()
        for error in (ValueError('GET failed with HTTP 500'), TimeoutError('Scan request timed out'),
                      IncompleteRead(self.scan(), 100), HTTPException('Broken scan response')):
            with self.subTest(error=error):
                self.http.responses[self.listing + '/versions'] = error
                self.http.calls.clear()
                message = self.run_publish(preflight=False)
                self.assertIn('Already published; content and metadata verified', message)
                self.assertIn('Virus scan: pending (status unavailable)', message)
                self.assertEqual(self.http.calls.count(self.listing + '/versions'), 1)
        self.assertFalse(self.sleeps)
        self.assertFalse(self.uploads)
        self.assertFalse(self.metadata.calls)

    def test_reported_virus_detections_still_fail_publication(self):
        self.ready()
        for page in (self.scan(status='detected'), self.scan() + self.scan(status='detected')):
            self.http.responses[self.listing + '/versions'] = page
            self.assert_rejected('Hexium reports virus detections for 1.2.3')
        self.assertFalse(self.sleeps)

    def test_virus_detections_for_another_version_do_not_affect_requested_scan(self):
        self.ready()
        self.http.responses[self.listing + '/versions'] = self.scan(version='1.2.4', status='detected') + self.scan()
        self.assertIn('Virus scan: successful', self.run_publish(preflight=False))
        self.assertFalse(self.uploads)

    def test_non_404_lookup_errors_never_allow_upload(self):
        self.http.responses[self.version_url] = ValueError('HTTP 403')
        self.assert_rejected('HTTP 403')

    def test_reconciliation_deadline_accounts_for_time_spent_on_requests(self):
        self.ready()
        self.http.responses[self.package_url] = None
        ticks = iter((0, 295, 305))
        with self.assertRaisesRegex(hexium.Pending, 'publication incomplete'):
            self.run_publish(preflight=False, now=lambda: next(ticks))
        self.assertEqual(self.sleeps, [5])

    def test_cli_defaults_to_preflight_and_passes_upload_switch(self):
        for args, preflight in (([], True), (['--upload'], False)):
            with patch.dict(os.environ, {'HEXIUM_NAMESPACE': 'randenko', 'HEXIUM_API_TOKEN': 'fixture',
                                         'HEXIUM_PUBLISH_ENABLED': 'true', 'GITHUB_STEP_SUMMARY': ''}), \
                    patch.object(sys, 'argv', ['publish_hexium.py', 'v1.2.3', *args]), \
                    patch.object(hexium, 'publish', return_value='Verified') as publish, \
                    patch('sys.stdout', new_callable=io.StringIO):
                hexium.main()
            self.assertEqual(publish.call_args.kwargs['enabled'], 'true')
            self.assertEqual(publish.call_args.kwargs['preflight'], preflight)


class HexiumHttpTests(unittest.TestCase):
    def test_metadata_response_read_errors_require_reconciliation(self):
        metadata = hexium.MetadataHttp('private-fixture')
        for error in (IncompleteRead(b'private-fixture', 100), HTTPException('private-fixture'),
                      TimeoutError('private-fixture'), URLError('private-fixture')):
            with self.subTest(error=error), patch.object(metadata.opener, 'open') as open_url:
                open_url.return_value.__enter__.return_value.read.side_effect = error
                with self.assertRaisesRegex(ValueError, 'response was lost; retry this release tag to reconcile') as caught:
                    metadata.update('randenko', 'GuildChest')
                self.assertNotIn('private-fixture', str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(open_url.call_count, 1, 'A lost write response must not resubmit automatically.')

    def test_metadata_write_is_fixed_origin_and_token_only_in_header(self):
        metadata = hexium.MetadataHttp('private-fixture')
        with patch.object(metadata.opener, 'open') as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{}'
            metadata.update('randenko', 'GuildChest')
            request = open_url.call_args.args[0]
            self.assertEqual(request.full_url, f'{hexium.HEXIUM}/api/mod/randenko/GuildChest/set-metadata')
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.get_header('Authorization'), 'Bearer private-fixture')
            self.assertEqual(json.loads(request.data), {'install_location': 'both', 'categories': list(hexium.CATEGORIES)})
            with self.assertRaisesRegex(ValueError, 'Invalid Hexium metadata target'):
                metadata.update('../unsafe', 'GuildChest')
            self.assertEqual(open_url.call_count, 1)

    def test_metadata_redirects_are_never_followed_even_on_same_origin(self):
        for target in (hexium.HEXIUM + '/other', 'https://cdn.hexium.gg/file'):
            with self.assertRaisesRegex(ValueError, 'must not redirect'):
                hexium.NoRedirect().redirect_request(Request(hexium.HEXIUM), None, 302, '', {}, target)

    def test_get_transport_scopes_github_credential_and_refreshes_public_metadata(self):
        http = hexium.HexiumHttp('github-private-fixture')
        with patch.object(http.opener, 'open') as open_url, patch.object(hexium.time, 'time_ns', side_effect=(123, 456)):
            open_url.return_value.__enter__.return_value.read.return_value = b'{}'
            for url in (hexium.HEXIUM + '/api/experimental/package/randenko/GuildChest/',
                        hexium.HEXIUM + '/mods/randenko/GuildChest/versions',
                        'https://cdn.hexium.gg/upload/1/1.2.3.zip', 'https://api.github.com/assets/1'):
                http.json(url)
            requests = [call.args[0] for call in open_url.call_args_list]
            self.assertTrue(all(request.get_method() == 'GET' for request in requests))
            self.assertTrue(all(request.get_header('Authorization') is None for request in requests[:-1]))
            self.assertEqual(requests[-1].get_header('Authorization'), 'Bearer github-private-fixture')
            self.assertTrue(requests[0].full_url.endswith('?guildchest_refresh=123'))
            self.assertTrue(requests[1].full_url.endswith('?guildchest_refresh=456'))
        original = Request('https://api.github.com/assets/1', headers={'Authorization': 'Bearer github-private-fixture'})
        redirected = CredentialRedirect().redirect_request(original, None, 302, '', {}, 'https://cdn.hexium.gg/file')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_only_404_is_absence(self):
        http = hexium.HexiumHttp()
        for code in (404, 401, 429, 500):
            with patch.object(http.opener, 'open', side_effect=HTTPError(hexium.HEXIUM, code, 'error', {}, None)):
                if code == 404:
                    self.assertIsNone(http.json(hexium.HEXIUM, optional=True))
                else:
                    with self.assertRaisesRegex(ValueError, f'HTTP {code}'):
                        http.json(hexium.HEXIUM, optional=True)


if __name__ == '__main__':
    unittest.main()
