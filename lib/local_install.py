"""Download and verify the standalone CtrlB binary using the standard library."""
import argparse
import hashlib
import http.client
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

MAX_BINARY_BYTES = 512 * 1024 * 1024
RELEASES = 'https://github.com/ctrlb-hq/ctrlb-agent/releases'


class InstallError(Exception):
    pass


def target_name():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'amd64': 'amd64', 'x86_64': 'amd64'}.get(machine)
    if system not in ('darwin', 'linux') or not arch:
        raise InstallError('unsupported platform: CtrlB requires macOS or Linux on arm64 or amd64')
    return f'ctrlb-{system}-{arch}'


def release_base(version):
    """GitHub serves the newest release and a named release at different paths."""
    if version == 'latest':
        return RELEASES + '/latest/download'
    # The version is one path segment of the release address: a tag such as v0.1.0.
    if not re.fullmatch(r'[A-Za-z0-9._+-]+', version) or version in ('.', '..'):
        raise InstallError('--version must be a release tag such as v0.1.0 '
                           '(letters, digits, dot, underscore, plus or hyphen)')
    return RELEASES + '/download/' + version


def validate_url(url, redirect=False):
    """Allow HTTPS, local file fixtures, and HTTP on the two loopback hosts.

    A redirect target may carry a query, because a release host signs its file
    addresses that way. It may not be a file.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # Reject malformed ports before opening the URL.
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if not redirect and (parsed.query or parsed.fragment):
            raise ValueError
        if (not redirect and parsed.scheme == 'file' and parsed.netloc in ('', 'localhost')
                and parsed.path.startswith('/')):
            return
        if parsed.hostname and (parsed.scheme == 'https' or
                                (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost'))):
            return
    except ValueError:
        pass
    if redirect:
        raise InstallError('the download was redirected to an address that is not allowed; nothing installed')
    raise InstallError('download URL must use HTTPS (file:// and HTTP on 127.0.0.1 or localhost are allowed)')


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, redirect=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    validate_url(url)
    return urllib.request.build_opener(_SafeRedirect()).open(url, timeout=60)


def _checksum(base, filename):
    try:
        with _open(base + '/SHA256SUMS') as response:
            manifest = response.read(1024 * 1024 + 1)
        if len(manifest) > 1024 * 1024:
            raise InstallError('SHA256SUMS is too large; nothing installed')
        lines = manifest.decode('ascii').splitlines()
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise InstallError(f'could not download or read SHA256SUMS from {base}; nothing installed') from None
    matches = []
    for line in lines:
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        if match and match.group(2) == filename:
            matches.append(match.group(1).lower())
    if not matches:
        raise InstallError(f'missing checksum for {filename} in SHA256SUMS; nothing installed')
    if len(matches) > 1:
        raise InstallError(f'duplicate checksum for {filename} in SHA256SUMS; nothing installed')
    return matches[0]


def install_binary(base, home, port=None):
    """Verify the raw binary, replace it atomically, then initialize local config."""
    filename = target_name()
    base = base.rstrip('/')
    validate_url(base)
    expected = _checksum(base, filename)
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(home, os.stat(home).st_mode & 0o700)
    bindir = os.path.join(home, 'bin')
    os.makedirs(bindir, mode=0o700, exist_ok=True)
    os.chmod(bindir, os.stat(bindir).st_mode & 0o700)
    destination = os.path.join(bindir, 'ctrlb')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.ctrlb-download-', dir=bindir, delete=False) as output:
            temporary = output.name
            digest = hashlib.sha256()
            downloaded = 0
            try:
                with _open(base + '/' + filename) as response:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_BINARY_BYTES:
                            raise InstallError('download too large; nothing installed')
                        output.write(chunk)
                        digest.update(chunk)
            except (OSError, ValueError, http.client.HTTPException):
                raise InstallError('could not download the CtrlB binary; nothing installed') from None
        if digest.hexdigest() != expected:
            raise InstallError('SHA-256 checksum mismatch; download deleted, nothing installed')
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
    command = [destination, 'init']
    if port is not None:
        command += ['--port', str(port)]
    # init may print a local key. Only the user's later export commands read it.
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=dict(os.environ, CTRLB_HOME=home), check=False)
    except OSError:
        raise InstallError('binary installed, but ctrlb init could not run; run it in your terminal') from None
    if result.returncode:
        raise InstallError('binary installed, but ctrlb init failed; run it in your terminal')
    return destination


def print_next_steps():
    print('Local CtrlB is installed and initialized. Add its bin folder to PATH, then install your agent hooks:')
    print('  claude plugin marketplace add ctrlb-hq/ctrlb-agent')
    print('  claude plugin install ctrlb-agent@ctrlb-agent')
    print('\nLoad these lines in your shell before starting your agent:')
    for variable, field in [('CTRLB_MCP_URL', 'mcp_url'), ('CTRLB_KEY', 'key')]:
        code = ('import json, os; print(json.load(open(os.path.join(os.environ.get("CTRLB_HOME") '
                'or os.path.expanduser("~/.ctrlb"), "config.json")))["' + field + '"])')
        print(f'  export {variable}="$(python3 -I -c \'{code}\')"')
    print('\nFor Codex, run from this clone: python3 -I install.py --codex')
    print('Start Codex and run /hooks once to trust the hooks. Session start brings up the local server.')


def main(argv):
    parser = argparse.ArgumentParser(prog='install.py --local', description=__doc__)
    parser.add_argument('--version', default='latest',
                        help='release to install, for example v0.1.0 (default: latest, the newest release)')
    parser.add_argument('--download-url',
                        help='full release base URL in place of the GitHub release (or CTRLB_DOWNLOAD_URL); '
                             '<version> in it is replaced with --version')
    parser.add_argument('--port', type=int, help='local server port (default: 7531 on first init)')
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    version = args.version or 'latest'
    base = args.download_url or os.environ.get('CTRLB_DOWNLOAD_URL')
    home = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
    try:
        if base:
            base = base.replace('<version>', urllib.parse.quote(version, safe=''))
        else:
            base = release_base(version)
        install_binary(base, home, args.port)
    except InstallError as error:
        print(f'CtrlB local install: {error}', file=sys.stderr)
        return 1
    except OSError:
        print('CtrlB local install: could not write the installation files', file=sys.stderr)
        return 1
    print_next_steps()
    return 0
