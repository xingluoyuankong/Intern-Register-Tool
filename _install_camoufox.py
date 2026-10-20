"""从本地 zip 安装 Camoufox。

`camoufox fetch` 走 GitHub 直下 493MB 老被中断（IncompleteRead）。
这里先用 curl 分段把 zip 下好，再**拦截 `fetcher.download_file`** 改读本地文件，
其余（sha256 校验 / 解压 / 写 version.json / set_active）全走 Camoufox 原逻辑。
"""

import sys

sys.path.insert(0, ".")

from camoufox.multiversion import (  # noqa: E402
    get_active_path,
    get_cached_versions,
    install_versioned,
)
from camoufox.pkgman import CamoufoxFetcher  # noqa: E402

LOCAL = (sys.argv[1] if len(sys.argv) > 1
         else r"C:\Users\XZXyuan\AppData\Local\Temp\camoufox.zip")


def _from_local(file, url):
    with open(LOCAL, "rb") as src:
        file.write(src.read())
    file.seek(0)          # verify_sha256 会从头读
    return file


# 🔴 必须让 fetcher **自己**选版本（`fetch_latest` 会挑库支持的那个，
#    就是 `camoufox fetch` 当时选的 152.0.4-beta.31）。
#    手动挑缓存里最新的（v156）会和已下载的 zip 版本不一致 → sha256 校验失败。
# 直接用库期望的版本（`fetch_latest` 选的就是它），zip 也是按它的 URL 下的，
# 版本一致 → **保留官方 sha256 校验**（不要再跳过）。
fetcher = CamoufoxFetcher()
print("目标版本:", fetcher.verstr, flush=True)

fetcher.download_file = _from_local
install_versioned(fetcher, replace=True)
print("installed. active path:", get_active_path())

fetcher.download_file = _from_local
install_versioned(fetcher, replace=True)
print("installed. active path:", get_active_path())
