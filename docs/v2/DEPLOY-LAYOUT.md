# Site deploy directory (A4u)

The site configuration (`adapters.json`, `inventory.json`, ...) lives outside the repo, in the site deploy directory: one flat
directory on the store host (the site role of that name in `SLICES.md`) holding the site files and their manifest `SHA256SUMS`
in `sha256sum` text format: one line per file, `<64 lowercase hex digits>`, two spaces, `<name>`. A name has 1-128 characters of
`A-Za-z0-9._-`, starts with a letter or digit, appears once and is never `SHA256SUMS`. Any other line makes the manifest malformed.
Nothing is read from a subdirectory. Site files are UTF-8 text: the command runner decodes what `cat` prints as UTF-8, so other
bytes never match their sha256.

## Publish

After the owner's review, write the files first and the manifest straight after them, in the deploy directory (a load that runs
while a file and the manifest disagree is refused and does not fall back):

    sha256sum <files> > SHA256SUMS.new && mv SHA256SUMS.new SHA256SUMS

## Load

`load_site` (`flightctl/siteconfig.py`) reads `SHA256SUMS`, then each file it names, with `cat` on the store host within one time
budget, and uses a file only when its sha256 equals its manifest entry. A failed read, a missing file, a malformed manifest or a
mismatch is refused: nothing unverified is used and nothing falls back to the local copy. A fully verified read is kept as the
host's verified local copy (the files first, `SHA256SUMS` last). Only when the store host does not answer (a timeout, an
unreachable transport or no time left) does the host verify and use that local copy; if it is missing or changed, there is no start.

## Lane cards

`lane_occupancy` takes a lane's host, card UUIDs and noise thresholds from the loaded `inventory.json` only: its bytes must still
match the published sha256 and its `stage` must be `confirmed`. They never come from the probe's own output.

## Units

`render_unit(template, values)` (`flightctl/siteconfig.py`) fills the `@NAME@` placeholders of a unit template (`NAME` is capital
letters, digits and `_`, starting with a letter) from the site's values and returns the text. `values` must name exactly the
template's placeholders, and each value is 1 to 1024 characters of `A-Za-z0-9._/+:=,-`, so a value cannot end its line, add a
directive or use a specifier or a variable. Values that do not match the placeholders, or a value outside that alphabet or length,
raise `SiteConfigRefused`; a template that is not a `str` or values that are not a `dict` raise `TypeError`. It reads, writes and
installs nothing: rendering, installing and enabling are the deployment's (A-ASM).

The templates are in `units/host/`. Install each rendered file without the `.template` suffix as a user unit (`systemctl --user`)
of the executor's account. User units run without a login session only if that account has linger (A6's probe decides whether it
is needed).

| Installed unit | Placeholders | What it does |
| --- | --- | --- |
| `flightctl-executor.timer` | none | starts the service 1 s after the timer starts, then every 60 s |
| `flightctl-executor.service` | `FLIGHTCTL_PYTHON`, `FLIGHTCTL_CHECKOUT`, `FLIGHTCTL_STATE`, `FLIGHTCTL_HOST_ID`, `FLIGHTCTL_SITE_DIR` | the enforcer one shot (`Type=oneshot`, `TimeoutStartSec=50s`): the interpreter runs `flightctl/executor_stdio.py --enforce` from the checkout with the state path, host id and site directory; only the timer starts it |

The workload units `flightctl-<lane_id>-g<generation>.service` (A6) and the idle inhibitors
`flightctl-awake-<lane_id>-g<generation>.service` (A11) are transient units made by `systemd-run --user`: they are never templates.
