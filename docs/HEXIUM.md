# Hexium publishing

The independent **Hexium** workflow publishes stable GitHub releases under team
`randenko`. It downloads the reviewed GitHub ZIP and SHA-256 asset, validates
them with `scripts/release_assets.py`, and passes the original ZIP to official
**tcli 0.2.4** with `--file`. The CLI asset and SHA-256 are pinned to the same
values used by the Thunderstore publisher. Releases are never rebuilt or
repackaged for upload. Existing tags, assets, and published notes stay unchanged.

Guild Chest 1.1.0 is now public on
[Hexium](https://valheim.hexium.gg/mods/randenko/GuildChest). Its downloadable
archive passes content verification; both DLLs have assembly and file version
1.1.0.0. The categories and Client & Server requirement are complete. Hexium
currently reports **Virus scan —**. An unavailable scan is reported as a pending
follow-up; verified publication succeeds without waiting for it. Clean Gale
profile and gameplay checks remain manual. See the
[publication record](VALIDATION.md).

## Configuration and first publication

GitHub **Settings → Secrets and variables → Actions** must contain:

| Type | Name | Value / purpose |
| --- | --- | --- |
| Variable | `HEXIUM_NAMESPACE` | `randenko` |
| Variable | `HEXIUM_PUBLISH_ENABLED` | `true`; enabled by the owner after Actions preflight passed. |
| Secret | `HEXIUM_API_TOKEN` | API token belonging to team `randenko`. |

All three settings were present when implementation started; the token value
was not retrieved. Preflight checks configuration presence, public dependencies,
categories, and release assets using GET requests. Upload and metadata-write
authorization were established during the 1.1.0 publication.
Keep credentials in environment variables or GitHub Secrets, never arguments,
committed files, or chat messages.

Hexium also has a **team API upload visibility** setting. The owner supplied
Hexium's launch inbox notice confirming that API uploads are hidden by default
pending review unless the team turns that behavior off. The owner confirmed
changing team `randenko` to publish API uploads immediately. This setting
applies to the team's API uploads; GitHub's `HEXIUM_PUBLISH_ENABLED` repository
variable controls whether this workflow uploads at all. Manual preflight
remains read-only even when that variable is `true`.

1. Review and merge the workflow and helper into `main` after CI passes.
2. In **Actions → Hexium → Run workflow**, select `main`, enter **v1.1.0**,
   and leave **Validate without uploading** enabled. Preflight works while the
   publishing switch is false.
3. After that run passes, set **HEXIUM_PUBLISH_ENABLED** to **true**. Leave
   Thunderstore and Nexus Mods switches at their existing values.
4. Manually dispatch **v1.1.0** again with **Validate without uploading** disabled.
   Its earlier GitHub release event will not replay automatically.
   Hexium's default team API upload visibility setting made the launch listing
   **private**, returning 404 from the public package API. If this happens, open
   the existing listing and choose
   **⚙ → Make public**, then retry the workflow to complete metadata. Do not
   upload the archive again while that listing is private. Hexium's documented
   API does not expose a visibility control. The owner subsequently changed the
   team setting to publish API uploads immediately, removing this manual
   visibility step from the configured future release process.
5. Check the public listing, four screenshots, changelog, Jötunn dependency,
   client/server requirement, and version scan. Install using a clean Gale
   profile; verify BepInEx and Jötunn resolution, both Guild Chest DLLs at
   **1.1.0.0**, and a chest interaction in a disposable world.
6. The verified listing URL is included in the README and saved
   [YouTube description](media/youtube-description.txt). Updating the live video
   description remains manual. Record actual results in [validation](VALIDATION.md).

Future stable `release: published` events run Hexium when its own switch is
exactly `true`. Manual dispatch defaults to read-only preflight. Direct upload
also requires the switch, before any remote reads or CLI invocation:

```bash
python3 scripts/publish_hexium.py v1.1.0 --preflight
python3 scripts/publish_hexium.py v1.1.0 --upload --cli /path/to/tcli
```

All Hexium runs share one concurrency group without cancelling previous uploads.
The workflow checks out `main` with full history and read-only repository access.
Thunderstore and Nexus Mods publish independently and can be retried separately.

## Verification and recovery

Shared validation checks stable tags and main ancestry, published GitHub release
status, checksums, archive allowlist, icon, tagged manifest, immutable links, and
the packaged changelog. Dependencies come from the tagged ZIP; the exact pinned
Jötunn and BepInEx versions must be active on Hexium. Category discovery follows
Hexium's own `next` pagination field, with same-origin and cycle checks.

Hexium [documents](https://hexium.gg/packaging) automatically stripping the
BepInEx dependency. Its downloaded ZIP can therefore have a different hash.
Verification requires the exact file set, no duplicate, symlink, encrypted, or
oversized entries, and identical bytes for every file except `manifest.json`.
The parsed manifest must equal the original, allowing JSON formatting changes
and removal of only `denikson-BepInExPack_Valheim-<pinned version>`. Removing
Jötunn, changing metadata, or changing any DLL or other file stops publication.

After archive identity is established, the helper uses the
[metadata API](https://valheim.hexium.gg/api/docs/) to set installation location
to `both` and complete **Building**, **Storage**, **Quality of Life**, and
**Open Source** categories. It verifies both the response and the approved
public listing's categories, including **Client & Server**. Protected Hexium
categories can remain present.

Virus scanning is reported separately after content and metadata verification.
The helper reads the requested version's public scan status once. **Scan
successful** is reported as successful; a missing or pending result, an
unavailable versions page, or an error reading it (including a truncated HTTP
response) is reported as **pending** with a link for follow-up. These conditions
do not wait for a scan or fail an otherwise verified publication. An explicit
**detections reported** badge still
fails the run for review. A badge on another version does not affect the
requested version's result.

Existing matching versions skip upload and complete unfinished metadata. CLI
errors and timeouts trigger reconciliation because Hexium may have accepted
the upload before its response was lost. Public visibility, downloads, and
metadata are polled for up to five minutes. Unresolved publication fails as
incomplete; wait for visibility, then retry the same release tag. A known inactive
or not-yet-downloadable version never permits another upload in the same run.
Non-404 API errors stop rather than being treated as absence. Metadata writes
set fixed values and can be retried safely after a lost response.

The Actions summary reports the listing URL, source SHA-256, downloaded SHA-256,
content-verification result, and separate virus-scan status, including incomplete
reconciliation results. Preflight and incomplete publication report a scan as
**not checked**. A pending scan needs follow-up on Hexium's versions page;
re-uploading is unnecessary. Archive conflicts, missing dependencies, and
incomplete or conflicting metadata still fail the run.

No publishing token is sent on reads, downloads, or redirects. The GitHub token
is restricted to its API origin. tcli receives only the Hexium token through
`TCLI_AUTH_TOKEN` and selected runtime environment settings. The metadata token
is restricted to one fixed Hexium endpoint; metadata redirects are refused.
