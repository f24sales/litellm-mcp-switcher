# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Refuse to overwrite an already published monthly version."""
import json
import os
import urllib.error
import urllib.request

version = os.environ["RELEASE_VERSION"]
for page in range(1, 101):
    req = urllib.request.Request(
        "https://api.github.com/users/f24sales/packages/container/"
        f"litellm-mcp-switcher-safrano9999/versions?per_page=100&page={page}",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            entries = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and page == 1:
            break  # First publication of this package.
        raise SystemExit(f"Cannot verify release availability (HTTP {error.code})") from None
    if any(version in item["metadata"]["container"]["tags"] for item in entries):
        raise SystemExit("Release tag already exists; increment project.version before publishing")
    if len(entries) < 100:
        break
else:
    raise SystemExit("Package history exceeds the bounded release check")
print("Release version is available")
