# Site deploy directory (A4u)

The site configuration (`adapters.json`, `inventory.json`, ...) lives outside the repo, in the site deploy directory: one flat
directory on the store host (the site role of that name in `SLICES.md`) holding the site files and their manifest `SHA256SUMS`
in `sha256sum` text format: one line per file, `<64 lowercase hex digits>`, two spaces, `<name>`. A name has 1-128 characters of
`A-Za-z0-9._-`, starts with a letter or digit, appears once and is never `SHA256SUMS`. Any other line makes the manifest malformed.

## Publish

After the owner's review, write the files first and the manifest last, in the deploy directory:

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
