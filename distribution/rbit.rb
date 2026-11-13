# Homebrew formula TEMPLATE for rbit
#
# Not installable until the placeholders are filled in. Before the first release:
#   1. Replace <VERSION> with the real release version (tag without the "v" prefix).
#   2. Point url at the actual release tarball (sdist or a platform tarball).
#   3. Compute the checksum:   shasum -a 256 rbit-<VERSION>.tar.gz
#   4. For a strict-audit-clean formula, declare textual/rich/httpx as `resource`
#      blocks and switch install to `virtualenv_install_with_resources`.
#   5. Test locally:           brew install --build-from-source ./distribution/rbit.rb
#                              brew audit --strict ./distribution/rbit.rb

class Rbit < Formula
  desc "Next-Gen Full-Screen Terminal AI Coding Agent & CLI"
  homepage "https://github.com/DreyzeDev/rbit"
  url "https://github.com/DreyzeDev/rbit/releases/download/v<VERSION>/rbit-<VERSION>.tar.gz"
  version "<VERSION>"
  sha256 "<SHA256-PLACEHOLDER>"

  license "MIT"

  depends_on "python@3.12"

  def install
    venv = virtualenv_create(libexec, "python@3.12")
    system libexec/"bin/pip", "install", "--no-cache-dir", "-v", buildpath
    bin.install libexec/"bin/rbit"
  end

  test do
    system bin/"rbit", "--version"
  end
end
