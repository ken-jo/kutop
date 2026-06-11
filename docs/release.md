# Release and Distribution

The public distribution name and Python package namespace are `kutop`. Releases
install `kutop` as the primary command and `kubetop` as a compatibility alias.

## GitHub Repository

The repository should be named:

```text
ken-jo/kutop
```

If the repository still has the old name, rename it once:

```bash
gh repo rename kutop -y
git remote set-url origin git@github.com:ken-jo/kutop.git
```

The package metadata, README, Homebrew formula, and apt repository URLs already
point at `https://github.com/ken-jo/kutop`.

## PyPI

Create the PyPI project as `kutop` and configure a Trusted Publisher:

```text
Owner: ken-jo
Repository name: kutop
Workflow name: release.yml
Environment name: pypi
```

The release workflow publishes to PyPI through OIDC; no PyPI API token is needed
when the Trusted Publisher is configured.

Enable publishing after the Trusted Publisher is ready:

```text
PUBLISH_PYPI=true
```

## Homebrew Tap

Create a tap repository, for example:

```bash
gh repo create ken-jo/homebrew-kutop --public
```

Configure repository variables/secrets on `ken-jo/kutop`:

```text
PUBLISH_HOMEBREW=true
HOMEBREW_TAP_REPO=ken-jo/homebrew-kutop
HOMEBREW_TAP_TOKEN=<token with write access to the tap repo>
```

Tagged releases build `kutop-homebrew-<version>.tar.gz`, render
`Formula/kutop.rb`, publish the tarball to the GitHub Release, and push the
formula into the tap repo.

Install command:

```bash
brew tap ken-jo/kutop
brew install kutop

# One-shot install without a separate tap step:
brew install ken-jo/kutop/kutop
```

## Apt Repository

The release workflow can publish a signed apt repository to GitHub Pages at:

```text
https://ken-jo.github.io/kutop/apt
```

Enable GitHub Pages with "GitHub Actions" as the source, then configure:

```text
PUBLISH_APT=true
APT_GPG_PRIVATE_KEY=<ASCII-armored private signing key>
APT_GPG_PASSPHRASE=<passphrase, or empty if the key has none>
APT_GPG_KEY_ID=<optional key id; auto-detected when omitted>
```

Install command:

```bash
curl -fsSL https://ken-jo.github.io/kutop/apt/kutop.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/kutop-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/kutop-archive-keyring.gpg] https://ken-jo.github.io/kutop/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/kutop.list
sudo apt update
sudo apt install kutop
```

## Release Process

1. Update `version` in `pyproject.toml` and `__version__` in `kutop/__init__.py`
   (they must match; the release workflow reads the former, `kutop --version`
   reports the latter).
2. Roll `CHANGELOG.md`: move the `## Unreleased` entries under a new
   `## X.Y.Z - <date>` heading and leave a fresh empty `## Unreleased` above it.
   (The GitHub Release body is auto-generated as `Release vX.Y.Z` — it is *not*
   sourced from the changelog, so edit the release notes by hand if you want
   them to mirror the changelog.)
3. Land the release commit on `master` (push directly, or merge a
   `release/vX.Y.Z` branch). The workflow triggers on the tag from any branch,
   but the released commit should live on `master`.
4. Create and push a matching tag (use the real version — the example below is
   a placeholder, and a tag that already exists is rejected):

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Pushing the tag starts `release.yml`. With the current repository variables
(`PUBLISH_PYPI=true`, `PUBLISH_HOMEBREW=true`, `PUBLISH_APT` unset) a release
publishes to **PyPI and Homebrew**; the apt job is skipped until apt is enabled
(see the Apt Repository section above).

The tag triggers `.github/workflows/release.yml`, which builds:

```text
dist/kutop-<version>.tar.gz
dist/kutop-<version>-py3-none-any.whl
dist/kutop_<version>_all.deb
dist/kutop-homebrew-<version>.tar.gz
Formula/kutop.rb
```
