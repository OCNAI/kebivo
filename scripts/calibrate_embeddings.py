#!/usr/bin/env python3
"""Score the bundled synthetic corpus against an explicitly configured endpoint."""

import argparse
import asyncio

from kebivo.dedup.evaluation import corpus, score_pairs, table
from kebivo.models import LlmProviderConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="OpenAI-compatible API base URL")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dims", type=int, required=True)
    parser.add_argument("--api-key-env", help="environment variable containing API key")
    args = parser.parse_args()
    provider = LlmProviderConfig(
        name="calibration",
        provider_type="local",
        base_url=args.url,
        model=args.model,
        headers_json="[]",
        timeout_seconds=60,
        allow_private_network=True,
        api_key_env=args.api_key_env,
    )
    pairs = corpus()
    scores = asyncio.run(score_pairs(provider, {}, args.model, args.dims, pairs))
    print(f"model={args.model} pairs={len(pairs)}")
    print(table(scores))


if __name__ == "__main__":
    main()
