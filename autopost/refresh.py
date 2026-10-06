"""アクセストークンを延長して、新しい値をファイルに書く。画面には出さない。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .instagram import Instagram


def main() -> int:
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    out = os.environ.get("TOKEN_OUT", "").strip()
    if not token or not out:
        raise SystemExit("設定が足りない：IG_ACCESS_TOKEN と TOKEN_OUT")

    body = Instagram(token).refresh_token()
    new_token = body.get("access_token", "")
    if not new_token:
        raise SystemExit("応答に新しいトークンが入っていない")

    print(f"::add-mask::{new_token}")
    Path(out).write_text(new_token, encoding="utf-8")
    days = int(body.get("expires_in", 0)) // 86400
    print(f"トークンを延長した。有効期限まで約{days}日")
    return 0


if __name__ == "__main__":
    sys.exit(main())
