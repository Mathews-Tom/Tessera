# B-SYNC-1 - Snapshot Sync Load

Measures Tessera snapshot sync against a populated encrypted vault. The
benchmark creates a fresh vault, captures synthetic `project` facets, pushes
the SQLCipher snapshot through the sync primitive, pulls it to a separate
vault path, and verifies the restored audit chain.

Run the v0.5 dogfood gate locally:

```bash
uv run python docs/benchmarks/B-SYNC-1-snapshot-load/run.py --n-facets 50000
```

The default backend is `local-filesystem`, which exercises the same
`BlobStore` protocol used by the S3-compatible adapter without requiring
external credentials. Real hosted-object-store timing remains a dogfood
follow-up because it requires operator-owned S3-compatible credentials and
network conditions that this repository cannot reproduce.

The benchmark writes non-overwriting JSON results to `results/`.
