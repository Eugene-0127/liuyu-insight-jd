"""Start a dedicated Edge normally, then connect through a local CDP endpoint."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

CDP_URL = 'http://127.0.0.1:9222'


def version():
    with urlopen(CDP_URL + '/json/version', timeout=1) as response:
        return json.load(response)


def ensure_browser(root):
    root = Path(root)
    marker = root / 'browser-link.json'
    try:
        info = version()
    except Exception:
        info = None
    if info:
        try:
            own = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            own = {}
        if own.get('websocket') != info.get('webSocketDebuggerUrl'):
            raise RuntimeError('9222 端口已被其他程序使用；未接管该浏览器')
        return CDP_URL
    paths = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
             Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe']
    executable = next((p for p in paths if p.is_file()), None)
    if not executable:
        raise RuntimeError('未找到 Edge')
    root.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([str(executable), '--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
                      '--user-data-dir=' + str(root / 'browser-profile'), '--new-window', 'about:blank'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(.3)
        try:
            info = version()
            marker.write_text(json.dumps({'websocket': info['webSocketDebuggerUrl']}), encoding='utf-8')
            return CDP_URL
        except Exception:
            pass
    raise RuntimeError('无法连接专用 Edge；请关闭原采集窗口后重新打开')


if __name__ == '__main__':
    ensure_browser(Path(__file__).parent / 'data')
