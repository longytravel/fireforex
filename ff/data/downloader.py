"""Retired. Do not import.

The Dukascopy bar API returns null rows upstream (dukascopy_python 3.x
and 4.x both affected), so this wrapper is kept only as a tombstone
for `git log --follow`. Any import raises immediately so a stale .pyc
or a forgotten `from ff.data import downloader` can never silently
route today's traffic through the broken path.

Replacements:
  * M1 bars       → ff.data.m1_bi5_downloader.download
  * higher TFs    → ff.data.resample.derive_higher_tfs
"""

raise ImportError(
    "ff.data.downloader is retired. The Dukascopy bar API returns "
    "null rows upstream. Use ff.data.m1_bi5_downloader for M1 and "
    "ff.data.resample.derive_higher_tfs() for every higher TF."
)
