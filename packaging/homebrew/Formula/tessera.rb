# Homebrew formula for Tessera — the portable context layer.
#
# This file lives in-repo at `packaging/homebrew/Formula/tessera.rb` so
# the formula is versioned alongside the code it packages. The
# intended public install path is a dedicated tap repo:
#
#     brew tap Mathews-Tom/tessera
#     brew install tessera
#
# To create that tap, push a copy of this file as `Formula/tessera.rb`
# into a new GitHub repo at `Mathews-Tom/homebrew-tessera`. See the
# parent directory's README for the exact bootstrap steps.
#
# Until the tap repo exists, users can install directly from this
# file:
#
#     brew install --build-from-source packaging/homebrew/Formula/tessera.rb
#
# The formula ships a pre-release (0.1.0rc1). The explicit
# `==#{version}` pin in `pip_install` tells pip's resolver to accept
# the rc1 without requiring callers to pass `--pre`.

class Tessera < Formula
  include Language::Python::Virtualenv

  desc "Portable context layer for T-shaped AI-native users (encrypted SQLite, served over MCP)"
  homepage "https://github.com/Mathews-Tom/Tessera"
  url "https://files.pythonhosted.org/packages/source/t/tessera-context/tessera_context-0.1.0rc1.tar.gz"
  version "0.1.0rc1"
  sha256 "35371a146c54f1d76bfd9ae6ec338367beee3e484a010c8e67823ee93004ad4f"
  license "Apache-2.0"
  head "https://github.com/Mathews-Tom/Tessera.git", branch: "main"

  depends_on "python@3.12"
  # sqlcipher3 (the non-Linux runtime dep pinned in pyproject.toml)
  # compiles its C extension against libsqlcipher. Linux users get
  # manylinux wheels of sqlcipher3-binary and never hit this dep; on
  # macOS the source install needs Homebrew's sqlcipher to link.
  depends_on "sqlcipher"

  def install
    venv = virtualenv_create(libexec, "python3.12")

    # sqlcipher3's setup.py probes CFLAGS / LDFLAGS and LIBSQLCIPHER_PATH
    # when locating headers and libs. Pointing them at Homebrew's
    # sqlcipher prefix is the documented install path for the package
    # on macOS and avoids the default /usr/local/include probe, which
    # is incorrect on Apple Silicon where Homebrew lives in
    # /opt/homebrew instead.
    sqlcipher_prefix = Formula["sqlcipher"].opt_prefix
    ENV.append "CFLAGS", "-I#{sqlcipher_prefix}/include"
    ENV.append "LDFLAGS", "-L#{sqlcipher_prefix}/lib"
    ENV["LIBSQLCIPHER_PATH"] = sqlcipher_prefix.to_s

    # Install tessera-context and its runtime graph into the formula's
    # private venv. We pin the version exactly rather than using a
    # range so the formula ships a reproducible build; bumping is a
    # one-line edit plus sha256 refresh.
    venv.pip_install "tessera-context==#{version}"

    # Expose the console_scripts entry point (`tessera`) on $PATH.
    # The CLI binary lives at libexec/bin/tessera inside the venv;
    # Homebrew users invoke it via the symlink in HOMEBREW_PREFIX/bin.
    bin.install_symlink libexec/"bin/tessera"
  end

  test do
    # --help must exit 0 and print the command description. A broken
    # install (missing adapter dep, bad import path, sqlcipher3 link
    # failure) would surface as an import error here rather than
    # silently ship broken.
    output = shell_output("#{bin}/tessera --help")
    assert_match "tessera", output.downcase

    # doctor --help exercises the cli.doctor_cmd import path, which
    # pulls in the adapter registry — the module most likely to
    # regress when a transitive runtime dep (ollama, sqlite-vec,
    # tiktoken, sentence-transformers) is missing from the venv.
    assert_match "doctor", shell_output("#{bin}/tessera doctor --help").downcase
  end
end
