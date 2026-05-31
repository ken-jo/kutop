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
gh repo create ken-jo/homebrew-tap --public
```

Configure repository variables/secrets on `ken-jo/kutop`:

```text
PUBLISH_HOMEBREW=true
HOMEBREW_TAP_REPO=ken-jo/homebrew-tap
HOMEBREW_TAP_TOKEN=<token with write access to the tap repo>
```

Tagged releases build `kutop-homebrew-<version>.tar.gz`, render
`Formula/kutop.rb`, publish the tarball to the GitHub Release, and push the
formula into the tap repo.

Install command:

```bash
brew install ken-jo/tap/kutop
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

1. Update `version` in `pyproject.toml`.
2. Commit and push to `master`.
3. Create and push a matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag triggers `.github/workflows/release.yml`, which builds:

```text
dist/kutop-<version>.tar.gz
dist/kutop-<version>-py3-none-any.whl
dist/kutop_<version>_all.deb
dist/kutop-homebrew-<version>.tar.gz
Formula/kutop.rb
```
