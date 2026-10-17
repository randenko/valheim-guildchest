"""Exercise release gates against real disposable Git histories."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'scripts'))
from check_release import check_release, check_release_notes


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release-test@example.invalid')
        self.git('commit', '--allow-empty', '-m', 'Initial commit')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.notes = self.root / 'docs/releases/v1.2.3.md'
        self.notes.parent.mkdir(parents=True)
        self.notes.write_text('## Mod changes\n\n- A mod fix.\n\n'
                              '## Steam reference recovery\n\n- Recover incomplete downloads.\n\n'
                              '## Contributor documentation\n\n- Explain the development setup.\n')

    def git(self, *arguments):
        return subprocess.run(['git', *arguments], cwd=self.root, capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_matching_tag_on_main_is_accepted(self):
        self.assertEqual(check_release('v1.2.3', '1.2.3', self.root), '1.2.3')

    def test_older_commit_already_merged_into_main_is_accepted(self):
        release_commit = self.git('rev-parse', 'HEAD')
        self.git('commit', '--allow-empty', '-m', 'Later main commit')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('checkout', '--detach', release_commit)
        self.assertEqual(check_release('v1.2.3', '1.2.3', self.root), '1.2.3')

    def test_mismatched_manifest_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'does not match manifest version'):
            check_release('v1.2.4', '1.2.3', self.root)

    def test_malformed_and_prerelease_tags_are_rejected(self):
        for tag in ('1.2.3', 'v1.2', 'v1.2.3-beta.1', 'v01.2.3', 'v1.2.3\n'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'form vX.Y.Z'):
                check_release(tag, '1.2.3', self.root)

    def test_unmerged_commit_is_rejected(self):
        self.git('checkout', '-b', 'unmerged-release')
        self.git('commit', '--allow-empty', '-m', 'Unmerged change')
        with self.assertRaisesRegex(ValueError, 'must belong to origin/main'):
            check_release('v1.2.3', '1.2.3', self.root)

    def test_missing_main_reference_is_rejected(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        with self.assertRaisesRegex(ValueError, 'Cannot verify origin/main'):
            check_release('v1.2.3', '1.2.3', self.root)

    def test_missing_release_notes_are_rejected(self):
        self.notes.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing release notes'):
            check_release('v1.2.3', '1.2.3', self.root)

    def test_missing_or_misplaced_mod_changes_are_rejected(self):
        for notes in ('', '# Changes\n\n- A fix.\n', '## What changed\n\n- A fix.\n',
                      '## Publishing\n\n- A fix.\n\n## Mod changes\n\n- A fix.\n'):
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, 'must start with ## Mod changes'):
                self.notes.write_text(notes)
                check_release('v1.2.3', '1.2.3', self.root)

    def test_categories_are_optional_and_their_names_and_order_follow_the_release(self):
        for categories in ((), ('Steam reference recovery',),
                           ('Steam reference recovery', 'Contributor documentation'),
                           ('Contributor documentation', 'Steam reference recovery'), ('C#',)):
            with self.subTest(categories=categories):
                self.notes.write_text('# Guild Chest 1.2.3\n\n## Mod changes\n\n- A mod fix.\n' +
                                      ''.join(f'\n## {category}\n\n- A related change.\n' for category in categories))
                self.assertEqual(check_release('v1.2.3', '1.2.3', self.root), '1.2.3')

    def test_duplicate_categories_are_rejected(self):
        for categories in (('Mod changes',), ('mod CHANGES',),
                           ('Steam reference recovery', 'Steam reference recovery'),
                           ('Steam reference recovery', ' steam REFERENCE recovery \t')):
            with self.subTest(categories=categories), self.assertRaisesRegex(ValueError, 'Duplicate release notes category'):
                self.notes.write_text('## Mod changes\n\n- A mod fix.\n' +
                                      ''.join(f'\n## {category}\n\n- A related change.\n' for category in categories))
                check_release('v1.2.3', '1.2.3', self.root)

    def test_blank_category_headings_are_rejected(self):
        for heading in ('##', '## ', '## \t '):
            with self.subTest(heading=heading), self.assertRaisesRegex(ValueError, 'headings must not be empty'):
                self.notes.write_text(f'## Mod changes\n\n- A mod fix.\n\n{heading}\n\n- A build fix.\n')
                check_release('v1.2.3', '1.2.3', self.root)

    def test_closing_heading_markers_cannot_disguise_empty_or_duplicate_categories(self):
        for notes in ('## Mod changes ##\n\n- A mod fix.\n',
                      '## Mod changes\n\n- A mod fix.\n\n## Publishing\n\n- A fix.\n\n'
                      '## Publishing ##\n\n- Another fix.\n',
                      '## Mod changes\n\n- A mod fix.\n\n## ###\n\n- A fix.\n',
                      '## Mod changes\n\n- A mod fix.\n\n## Publishing ## \t\n\n- A fix.\n'):
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, 'closing # markers'):
                self.notes.write_text(notes)
                check_release('v1.2.3', '1.2.3', self.root)

    def test_sections_without_bullets_are_rejected(self):
        for mod, tooling in (('', '- A build fix.'), ('- A mod fix.', ''),
                             ('- ', '- A build fix.'), ('- A mod fix.', 'Plain text without a bullet.')):
            with self.subTest(mod=mod, tooling=tooling), self.assertRaisesRegex(ValueError, 'needs at least one bullet'):
                self.notes.write_text(f'## Mod changes\n\n{mod}\n\n## Steam reference recovery\n\n{tooling}\n')
                check_release('v1.2.3', '1.2.3', self.root)

    def test_mod_changes_with_explicit_none_are_accepted(self):
        self.notes.write_text('## Mod changes\n\n- None.\n')
        self.assertEqual(check_release('v1.2.3', '1.2.3', self.root), '1.2.3')

    def test_legacy_other_changes_category_remains_accepted(self):
        self.notes.write_text('## Mod changes\n\n- None.\n\n## Other changes\n\n- None.\n')
        self.assertEqual(check_release('v1.2.3', '1.2.3', self.root), '1.2.3')

    def test_existing_release_notes_remain_valid(self):
        for notes in sorted((PROJECT / 'docs/releases').glob('v*.md')):
            with self.subTest(tag=notes.stem):
                check_release_notes(notes.stem, PROJECT)


if __name__ == '__main__':
    unittest.main()
