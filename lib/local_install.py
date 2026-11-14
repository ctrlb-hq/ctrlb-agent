"""Download and verify the standalone CtrlB binary using the standard library."""
import argparse
import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


class InstallError(Exception):
    pass


def target_name():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'amd64': 'amd64', 'x86_64': 'amd64'}.get(machine)
    if system not in ('darwin', 'linux') or not arch:
        raise InstallError('unsupported platform: CtrlB requires macOS or Linux on arm64 or amd64')
    return f'ctrlb-{system}-{arch}'


def validate_url(url):
    """Allow HTTPS, local file fixtures, and HTTP on the two loopback hosts."""
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # Reject malformed ports before opening the URL.
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.scheme == 'file' and parsed.netloc in ('', 'localhost') and parsed.path.startswith('/'):
            return
        if parsed.hostname and (parsed.scheme == 'https' or
                                (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost'))):
            return
    except ValueError:
        pass
    raise InstallError('download URL must use HTTPS (file:// and HTTP on 127.0.0.1 or localhost are allowed)')


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
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
    except (OSError, ValueError, UnicodeError):
        raise InstallError('could not download or read SHA256SUMS; nothing installed') from None
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
    bindir = os.path.join(home, 'bin')
    os.makedirs(bindir, mode=0o700, exist_ok=True)
    destination = os.path.join(bindir, 'ctrlb')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.ctrlb-download-', dir=bindir, delete=False) as output:
            temporary = output.name
            digest = hashlib.sha256()
            try:
                with _open(base + '/' + filename) as response:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
            except (OSError, ValueError):
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
    parser.add_argument('--version', default='latest', help='replace <version> in the download URL (default: latest)')
    parser.add_argument('--download-url', help='full release base URL (or CTRLB_DOWNLOAD_URL)')
    parser.add_argument('--port', type=int, help='local server port (default: 7531 on first init)')
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    base = args.download_url or os.environ.get('CTRLB_DOWNLOAD_URL')
    if not base:
        print('CtrlB local install: supply --download-url or CTRLB_DOWNLOAD_URL; '
              'the release download address has not been published yet', file=sys.stderr)
        return 1
    base = base.replace('<version>', urllib.parse.quote(args.version, safe=''))
    home = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
    try:
        install_binary(base, home, args.port)
    except InstallError as error:
        print(f'CtrlB local install: {error}', file=sys.stderr)
        return 1
    except OSError:
        print('CtrlB local install: could not write the installation files', file=sys.stderr)
        return 1
    print_next_steps()
    return 0
