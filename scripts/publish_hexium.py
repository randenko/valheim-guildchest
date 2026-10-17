#!/usr/bin/env python3
"""Preflight or publish the original GitHub release ZIP to Hexium."""
import argparse
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from package_format import DEPENDENCY, VERSION, validate_archive
from release_assets import Http, load_release

ROOT = Path(__file__).resolve().parents[1]
HEXIUM = 'https://valheim.hexium.gg'
CATEGORIES = ('Building', 'Storage', 'Quality of Life', 'Open Source')
INSTALL_CATEGORIES = {'Client-only', 'Server-only', 'Client & Server', 'Client (& Server)'}
BEPINEX = 'denikson-BepInExPack_Valheim-'


class Pending(ValueError):
    """A known version or listing is not fully available yet; never reupload it."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Hexium metadata requests must not redirect.')


class MetadataHttp:
    """The publishing token is used only for this fixed-origin metadata write."""
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def update(self, namespace, name):
        if not all(re.fullmatch(r'[A-Za-z0-9_]+', value) for value in (namespace, name)):
            raise ValueError('Invalid Hexium metadata target.')
        url = f'{HEXIUM}/api/mod/{namespace}/{name}/set-metadata'
        body = json.dumps({'install_location': 'both', 'categories': CATEGORIES}).encode()
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json',
                   'Accept': 'application/json', 'User-Agent': 'GuildChest-release'}
        try:
            with self.opener.open(Request(url, data=body, headers=headers, method='POST'), timeout=60) as response:
                return json.loads(response.read())
        except HTTPError as error:
            raise ValueError(f'Hexium metadata update failed with HTTP {error.code}; retry this release tag.') from error
        except URLError as error:
            raise ValueError('Hexium metadata response was lost; retry this release tag to reconcile.') from error


class HexiumHttp(Http):
    def get(self, url, **kwargs):
        parts = urlsplit(url)
        if parts.netloc == 'valheim.hexium.gg':
            separator = '&' if parts.query else '?'
            url += f'{separator}guildchest_refresh={time.time_ns()}'
        return super().get(url, **kwargs)


def package_url(namespace, manifest):
    return f'{HEXIUM}/api/experimental/package/{namespace}/{manifest["name"]}/'


def check_availability(http, manifest):
    base = f'{HEXIUM}/api/experimental/community/valheim/category/'
    url, seen, slugs = base, set(), set()
    while url:
        parts = urlsplit(url)
        if (url in seen or parts.scheme != 'https' or parts.netloc != 'valheim.hexium.gg' or
                parts.path != urlsplit(base).path or parts.fragment):
            raise ValueError('Invalid Hexium category pagination URL.')
        seen.add(url)
        page = http.json(url)
        slugs.update(item['slug'] for item in page['results'])
        # Hexium uses next, independently of Thunderstore's pagination.next_link.
        url = urljoin(base, page['next']) if page['next'] else None
    missing = set(CATEGORIES) - slugs
    if missing:
        raise ValueError(f'Unavailable Hexium categories: {", ".join(sorted(missing))}.')
    for dependency in manifest['dependencies']:
        namespace, name, version = DEPENDENCY.fullmatch(dependency).groups()
        data = http.json(f'{HEXIUM}/api/experimental/package/{namespace}/{name}/{version}/', optional=True)
        if (not data or data.get('is_active') is not True or data.get('full_name') != dependency or
                (data.get('namespace'), data.get('name'), data.get('version_number')) != (namespace, name, version)):
            raise ValueError(f'Required Hexium dependency is unavailable or inactive: {dependency}.')


def strict_manifest(contents):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Hexium manifest contains duplicate keys.')
            result[key] = value
        return result
    return json.loads(contents, object_pairs_hook=unique_object)


def verify_contents(source, downloaded):
    """Allow JSON formatting and removal of only the pinned BepInEx dependency."""
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(io.BytesIO(downloaded)) as existing:
        validate_archive(existing)
        if set(existing.namelist()) != set(original.namelist()):
            raise ValueError('Existing Hexium archive has a different file set; stop publication.')
        for name in original.namelist():
            if name != 'manifest.json' and existing.read(name) != original.read(name):
                raise ValueError(f'Existing Hexium archive has different contents: {name}; stop publication.')
        manifest = strict_manifest(original.read('manifest.json'))
        normalized = {**manifest, 'dependencies': [item for item in manifest['dependencies']
                                                  if not item.startswith(BEPINEX)]}
        if strict_manifest(existing.read('manifest.json')) not in (manifest, normalized):
            raise ValueError('Existing Hexium manifest has unsupported metadata changes; stop publication.')
    return hashlib.sha256(downloaded).hexdigest()


def matching_version(http, namespace, release):
    data = http.json(package_url(namespace, release.manifest) + release.version + '/', optional=True)
    if data is None:
        return None
    if (data.get('namespace'), data.get('name'), data.get('version_number')) != (
            namespace, release.manifest['name'], release.version):
        raise ValueError('Hexium returned a different package version; stop publication.')
    if data.get('is_active') is not True or not data.get('download_url'):
        raise Pending('Existing Hexium version is inactive or has no download URL.')
    downloaded = http.get(data['download_url'], binary=True, optional=True)
    if downloaded is None:
        raise Pending('Hexium archive is not downloadable yet.')
    return verify_contents(release.contents, downloaded)


def public_metadata(http, namespace, manifest):
    data = http.json(package_url(namespace, manifest), optional=True)
    if data is None:
        raise Pending('Hexium public listing is not visible yet.')
    if (data.get('namespace'), data.get('name')) != (namespace, manifest['name']):
        raise ValueError('Hexium returned a different public listing; stop publication.')
    listings = [item for item in data['community_listings'] if item.get('community') == 'valheim']
    if len(listings) != 1 or listings[0].get('review_status') != 'approved':
        raise Pending('Hexium public listing is awaiting approval.')
    categories = set(listings[0]['categories'])
    return set(CATEGORIES) <= categories and categories & INSTALL_CATEGORIES == {'Client & Server'}


class VersionScan(HTMLParser):
    """Read only the scan badge inside the requested version's public card."""
    def __init__(self, version):
        super().__init__()
        self.version = version
        self.depth = 0
        self.target_depth = None
        self.clean = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            self.depth += 1
            if attrs.get('id') == f'v-{self.version}' and 'version-entry' in attrs.get('class', '').split():
                self.target_depth = self.depth
        if self.target_depth is not None and 'status-badge--vt-clean' in attrs.get('class', '').split():
            self.clean = True

    def handle_endtag(self, tag):
        if tag == 'div':
            if self.depth == self.target_depth:
                self.target_depth = None
            self.depth -= 1


def scan_complete(http, listing, version):
    page = http.get(listing + '/versions', optional=True)
    if page is None:
        raise Pending('Hexium versions page is not visible yet.')
    parser = VersionScan(version)
    parser.feed(page.decode('utf-8'))
    return parser.clean


def configuration(namespace, manifest):
    quote = json.dumps
    lines = ['[config]', 'schemaVersion = "0.0.1"', '', '[package]',
             f'namespace = {quote(namespace)}', f'name = {quote(manifest["name"])}',
             f'versionNumber = {quote(manifest["version_number"])}',
             f'description = {quote(manifest["description"])}',
             f'websiteUrl = {quote(manifest["website_url"])}', 'containsNsfwContent = false',
             '', '[package.dependencies]']
    for dependency in manifest['dependencies']:
        author, name, version = DEPENDENCY.fullmatch(dependency).groups()
        lines.append(f'{quote(author + "-" + name)} = {quote(version)}')
    lines.extend(['', '[build]', 'outdir = "unused"', 'copy = []', '', '[publish]',
                  f'repository = {quote(HEXIUM)}', 'communities = ["valheim"]',
                  '', '[publish.categories]', f'valheim = {quote(CATEGORIES)}', ''])
    return '\n'.join(lines)


def report(status, release, listing, downloaded=None):
    result = 'passed (all other files identical; manifest normalization allowed)' if downloaded else 'pending'
    return (f'{status}: {release.tag}\n\nListing: {listing}\n\n'
            f'Source SHA-256: {release.sha256}\n\n'
            f'Downloaded SHA-256: {downloaded or "not available"}\n\nContent verification: {result}.')


def publish(tag, *, repository=ROOT, namespace='', token='', http=None, metadata=None,
            preflight=True, enabled='', cli='tcli', run=subprocess.run, sleep=time.sleep, now=time.monotonic):
    if not re.fullmatch(f'v{VERSION}', tag):
        raise ValueError('Only stable vX.Y.Z release tags are accepted.')
    if not preflight and enabled != 'true':
        raise ValueError('HEXIUM_PUBLISH_ENABLED must be true for publishing.')
    if not re.fullmatch(r'[A-Za-z0-9_]+', namespace):
        raise ValueError('HEXIUM_NAMESPACE is missing or invalid.')
    if not token.strip():
        raise ValueError('HEXIUM_API_TOKEN is missing.')
    http = http or HexiumHttp(os.environ.get('GH_TOKEN', ''))
    release = load_release(tag, repository=repository, http=http)
    check_availability(http, release.manifest)
    listing = f'{HEXIUM}/mods/{namespace}/{release.manifest["name"]}'
    pending = ''
    try:
        downloaded = matching_version(http, namespace, release)
    except Pending as error:
        downloaded, pending = None, str(error)
    if preflight:
        if pending:
            raise Pending(report(f'Preflight incomplete: {pending}', release, listing))
        return report('Preflight passed (GET-only; upload authorization untested)', release, listing, downloaded)
    submitted = False
    cli_status = 'Existing version'
    if downloaded is None and not pending:
        with tempfile.TemporaryDirectory(prefix='guildchest-hexium-') as directory:
            archive = Path(directory) / release.filename
            archive.write_bytes(release.contents)
            config = Path(directory) / 'thunderstore.toml'
            config.write_text(configuration(namespace, release.manifest), encoding='utf-8')
            # Give tcli only runtime settings and its Hexium credential. In particular,
            # GitHub, Nexus, Thunderstore, and ambient TCLI_* settings cannot leak in.
            runtime = ('PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL',
                       'DOTNET_ROOT', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'SYSTEMROOT')
            environment = {key: os.environ[key] for key in runtime if key in os.environ}
            environment['TCLI_AUTH_TOKEN'] = token
            try:
                result = run([cli, 'publish', '--config-path', str(config), '--file', str(archive),
                              '--tcli-directory', str(Path(directory) / 'tcli-state')],
                             cwd=directory, env=environment, timeout=3600)
                cli_status = f'tcli exited {result.returncode}'
            except (subprocess.TimeoutExpired, OSError) as error:
                cli_status = f'tcli {type(error).__name__}'
            submitted = True
    metadata = metadata or MetadataHttp(token)
    deadline = now() + 300
    updated = False
    for attempt in range(31):
        try:
            if downloaded is None:
                downloaded = matching_version(http, namespace, release)
            if downloaded is None:
                raise Pending('Hexium version is not visible yet.')
            complete = public_metadata(http, namespace, release.manifest)
            if not complete and not updated:
                response = metadata.update(namespace, release.manifest['name'])
                if ((response.get('namespace'), response.get('name'), response.get('install_location')) !=
                        (namespace, release.manifest['name'], 'both') or
                        not set(CATEGORIES) <= set(response.get('categories', []))):
                    raise ValueError('Hexium metadata response does not match the requested listing.')
                updated = True
                complete = public_metadata(http, namespace, release.manifest)
            if not complete:
                raise Pending('Hexium listing metadata is not complete yet.')
            if not scan_complete(http, listing, release.version):
                raise Pending('Hexium version scan is not successful yet.')
            status = 'Published and verified' if submitted else 'Already published; metadata and scan verified'
            return report(status, release, listing, downloaded)
        except Pending as error:
            pending = str(error)
        remaining = deadline - now()
        if attempt == 30 or remaining <= 0:
            break
        sleep(min(10, remaining))
    raise Pending(report(f'{cli_status}; publication incomplete: {pending} Retry this release tag later',
                         release, listing, downloaded))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight', action='store_true', help='GET-only validation (default).')
    mode.add_argument('--upload', action='store_true', help='Publish the verified original release archive.')
    parser.add_argument('--cli', default='tcli')
    args = parser.parse_args()
    try:
        message = publish(args.tag, namespace=os.environ.get('HEXIUM_NAMESPACE', ''),
                          token=os.environ.get('HEXIUM_API_TOKEN', ''), preflight=not args.upload,
                          enabled=os.environ.get('HEXIUM_PUBLISH_ENABLED', ''), cli=args.cli)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        message = str(error)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
                output.write(message + '\n')
        raise SystemExit(message) from error
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
            output.write(message + '\n')


if __name__ == '__main__':
    main()
