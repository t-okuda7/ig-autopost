#!/bin/bash
# ig-autopost の初期設定を、手元のターミナルからまとめて行う。
#
#   bash setup.sh           設定 → 接続の確認 → 点検（何も投稿しない）
#   bash setup.sh check     接続の確認だけ
#   bash setup.sh dry-run   点検だけ（切り抜き後の写真を開く）
#   bash setup.sh post      Instagram に投稿する
#   bash setup.sh token     Instagram のトークンを作り直す（Meta の画面のボタンで作れないとき）
#
# 必要なもの：git、gh（GitHub CLI、ログイン済み）、gcloud（Google Cloud CLI、ログイン済み）
# gcloud が無い場合は、自分で作った鍵ファイルを渡せる：  KEY_FILE=~/Downloads/鍵.json bash setup.sh
#
# 鍵は画面に出さず、手元にも残さない。Google の鍵は作ってすぐ GitHub に渡して消す。
# 途中で止まっても、もう一度実行すれば済んだところは飛ばして続きから進む。
set -euo pipefail
cd "$(dirname "$0")"

LOCAL_ENV="setup.local.env"   # フォルダのIDなど、公開しない手元だけの設定（git には入れない）
STATE=".setup-state"
if [ -f "$LOCAL_ENV" ]; then . "./$LOCAL_ENV"; fi
if [ -f "$STATE" ]; then . "./$STATE"; fi

REPO_NAME="${REPO_NAME:-ig-autopost}"
SA_NAME="ig-autopost"
REPO="${REPO:-}"
SA_EMAIL="${SA_EMAIL:-}"
KEY_DIR=""
PUSH_HELP="GitHub に送れなかった。上に出ている英語の理由を見る。よくある原因は2つ：
  ・workflow の権限が無い →  gh auth refresh -s workflow
  ・git が GitHub にログインできていない →  gh auth setup-git
  どちらかを実行してから、もう一度  bash setup.sh"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31m止まりました：%s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local reply=""; read -r -p "  $1 [y/N] " reply || true; [ "$reply" = "y" ] || [ "$reply" = "Y" ]; }
cleanup() { if [ -n "$KEY_DIR" ] && [ -d "$KEY_DIR" ]; then rm -rf "$KEY_DIR"; fi; }
trap cleanup EXIT

save_state() {
  { printf 'REPO=%s\n' "$REPO"; printf 'SA_EMAIL=%s\n' "$SA_EMAIL"; } > "$STATE"
}

has_secret() {
  gh secret list --repo "$REPO" 2>/dev/null | awk -v name="$1" '$1 == name { found = 1 } END { exit !found }'
}

preflight() {
  command -v git >/dev/null 2>&1 || die "git が見つからない"
  command -v gh  >/dev/null 2>&1 || die "gh（GitHub CLI）が見つからない。  brew install gh  のあと  gh auth login"
  gh auth status >/dev/null 2>&1 || die "gh にログインしていない。  gh auth login  を先に実行する"
}

find_repo() {
  if [ -z "$REPO" ]; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)
  fi
  [ -n "$REPO" ] || die "リポジトリがまだ無い。先に  bash setup.sh  を実行する"
}

# ---------------------------------------------------------------- 1. GitHub

step_repo() {
  say "1/5  GitHub のリポジトリ"
  local top
  top=$(git rev-parse --show-toplevel 2>/dev/null || true)
  if [ -z "$top" ]; then
    git init -q -b main
  elif [ "$(cd "$top" && pwd -P)" != "$(pwd -P)" ]; then
    die "このフォルダは、別の git リポジトリ（$top）の中にある。そのリポジトリの外に移してから実行する"
  fi
  git add -A
  if ! git diff --cached --quiet; then
    git commit -q -m "ig-autopost" || die "commit できなかった。git の名前が未設定なら、次の2つを実行してからもう一度：
  git config --global user.name \"好きな名前\"
  git config --global user.email \"GitHubに登録したメール\""
  fi

  if git remote get-url origin >/dev/null 2>&1; then
    git push -q -u origin HEAD || die "$PUSH_HELP"
  else
    local owner
    owner=$(gh api user -q .login) || die "GitHub のユーザー名を取れなかった"
    if gh repo view "$owner/$REPO_NAME" >/dev/null 2>&1; then
      note "$owner/$REPO_NAME はすでにある。そこへ送る"
      if [ "$(gh config get git_protocol -h github.com 2>/dev/null || true)" = "ssh" ]; then
        git remote add origin "git@github.com:$owner/$REPO_NAME.git"
      else
        git remote add origin "https://github.com/$owner/$REPO_NAME.git"
      fi
      git push -q -u origin HEAD || die "$PUSH_HELP"
    else
      note "公開リポジトリ $owner/$REPO_NAME を作る。"
      note "コード、投稿直前の写真、点検で出る写真とキャプションが、誰からでも見える状態になる。鍵は見えない。"
      ask "作ってよいですか？" || die "中止した"
      gh repo create "$REPO_NAME" --public --source=. --remote=origin --push \
        || die "$PUSH_HELP"
    fi
  fi

  REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) || die "リポジトリを確認できなかった"
  local visibility
  visibility=$(gh repo view "$REPO" --json visibility -q .visibility 2>/dev/null || echo "")
  if [ "$visibility" != "PUBLIC" ]; then
    die "$REPO が公開（Public）になっていない。Instagram が写真を取りに来られないので、公開に変える必要がある"
  fi
  save_state
  note "✓ $REPO"
}

# ---------------------------------------------------------------- 2. Google

random_suffix() { od -An -N4 -tx1 /dev/urandom | tr -d ' \n'; }

step_google() {
  say "2/5  Google：Drive を読むための鍵"
  if has_secret GOOGLE_SERVICE_ACCOUNT_JSON; then
    note "✓ 登録済み（${SA_EMAIL:-サービスアカウントのアドレスは不明}）"
    return
  fi

  if [ -n "${KEY_FILE:-}" ]; then
    [ -f "$KEY_FILE" ] || die "KEY_FILE のファイルが見つからない：$KEY_FILE"
    SA_EMAIL=$(sed -n 's/.*"client_email"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$KEY_FILE" | sed -n '1p')
    [ -n "$SA_EMAIL" ] || die "鍵ファイルからサービスアカウントのアドレスを読めなかった"
    gh secret set GOOGLE_SERVICE_ACCOUNT_JSON --repo "$REPO" < "$KEY_FILE" || die "鍵を GitHub に登録できなかった"
    save_state
    note "✓ 登録した。手元の鍵ファイル（$KEY_FILE）は、もう要らないので消してよい"
    return
  fi

  command -v gcloud >/dev/null 2>&1 || die "gcloud が見つからない。入れる（ brew install --cask gcloud-cli のあと gcloud auth login ）か、
  Google Cloud の画面で鍵を作って  KEY_FILE=鍵のパス bash setup.sh  で渡す（手順は README の 2）"
  local account
  account=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | sed -n '1p')
  [ -n "$account" ] || die "gcloud にログインしていない。  gcloud auth login  を先に実行する"
  note "Google アカウント：$account"

  local project="${GCP_PROJECT_ID:-}"
  if [ -z "$project" ]; then project="ig-autopost-$(random_suffix)"; fi
  if gcloud projects describe "$project" >/dev/null 2>&1; then
    note "プロジェクト $project を使う"
  else
    note "プロジェクト $project を作る"
    gcloud projects create "$project" --name="ig-autopost" --quiet >/dev/null 2>&1 \
      || die "プロジェクトを作れなかった。https://console.cloud.google.com/ を一度開いて利用規約に同意してから、もう一度実行する。
  それでもだめなら、画面で鍵を作って  KEY_FILE=鍵のパス bash setup.sh  で渡す（手順は README の 2）"
  fi

  note "Google Drive API を有効にする"
  gcloud services enable drive.googleapis.com iam.googleapis.com --project "$project" --quiet >/dev/null 2>&1 \
    || die "Drive API を有効にできなかった（プロジェクト：$project）"

  SA_EMAIL="$SA_NAME@$project.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$project" >/dev/null 2>&1; then
    note "サービスアカウントを作る"
    gcloud iam service-accounts create "$SA_NAME" --project "$project" \
      --display-name "ig-autopost" --quiet >/dev/null 2>&1 || die "サービスアカウントを作れなかった"
  fi
  save_state

  note "鍵を作って、そのまま GitHub に渡す"
  KEY_DIR=$(mktemp -d)
  local key="$KEY_DIR/key.json" made="" attempt
  for attempt in 1 2 3 4 5 6; do
    if gcloud iam service-accounts keys create "$key" --iam-account "$SA_EMAIL" \
         --project "$project" --quiet >/dev/null 2>&1; then
      made="yes"; break
    fi
    sleep 5   # 作った直後は、少し待たないと鍵を作れないことがある
  done
  [ -n "$made" ] || die "鍵を作れなかった（$SA_EMAIL）"
  gh secret set GOOGLE_SERVICE_ACCOUNT_JSON --repo "$REPO" < "$key" || die "鍵を GitHub に登録できなかった"
  cleanup; KEY_DIR=""
  note "✓ 登録した。鍵は手元に残していない"
}

# ---------------------------------------------------------------- 3. フォルダのID

step_vars() {
  say "3/5  Drive のフォルダ"
  local name value
  for name in DRIVE_MATERIAL_ID DRIVE_DRAFT_ID DRIVE_POSTED_ID; do
    value="${!name:-}"
    if [ -z "$value" ]; then
      read -r -p "  $name（フォルダのURLの folders/ より後ろ）: " value
    fi
    [ -n "$value" ] || die "$name が空"
    gh variable set "$name" --repo "$REPO" --body "$value" >/dev/null || die "$name を登録できなかった"
  done
  note "✓ 3つ登録した"
}

# ---------------------------------------------------------------- 4. Instagram

step_token() {
  say "4/5  Instagram のトークン"
  if has_secret IG_ACCESS_TOKEN; then
    note "✓ 登録済み"
    return
  fi
  note "Meta の画面の「トークンを生成」でトークンが出たなら、それを貼り付ける。"
  note "出なかったなら、ブラウザで許可して作る（n を選ぶ）。"
  if ask "トークンをコピーしてある？"; then
    note "貼り付けて Enter。画面には表示されない。"
    gh secret set IG_ACCESS_TOKEN --repo "$REPO" || die "トークンを登録できなかった"
  else
    make_token
  fi
  note "✓ 登録した"
}

# Meta の画面のボタンを使わずに、ブラウザの許可からトークンを作って GitHub に登録する。
# トークンは画面に出さない。
json_field() { sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p'; }

show_error() {   # トークンが混じっていない返事だけを見せる
  case "$1" in
    *access_token*) note "（返事の中身は表示しない）" ;;
    *) note "Instagram の返事： $1" ;;
  esac
}

make_token() {
  command -v curl >/dev/null 2>&1 || die "curl が見つからない"
  local redirect="${IG_REDIRECT_URI:-https://localhost/}"
  local app_id="" secret="" encoded url pasted="" code reply short long

  note "Meta のアプリの画面「Instagramログインを使用したAPI設定」を開いておく。"
  note ""
  note "① その画面の「Instagramビジネスログインを設定する」の欄 →「ビジネスログイン設定」を開き、"
  note "   「OAuthリダイレクトURI」に  $redirect  を入れて保存する。"
  note "   投稿の権限（instagram_business_content_publish）がアプリに追加されていることも確かめる。"
  read -r -p "  保存したら Enter " _ || true

  note "② 同じところにある「InstagramアプリID」（数字。Meta のアプリIDとは別）"
  read -r -p "  InstagramアプリID: " app_id || true
  app_id=$(printf '%s' "$app_id" | tr -dc '0-9')
  [ -n "$app_id" ] || die "InstagramアプリID が空"

  note "③ その隣の「Instagram app secret」（「表示」を押してコピー）。貼り付けて Enter。画面には表示されない。"
  read -r -s -p "  app secret: " secret || true
  printf '\n'
  secret=$(printf '%s' "$secret" | tr -d '[:space:]')
  [ -n "$secret" ] || die "app secret が空"

  encoded=$(printf '%s' "$redirect" | sed 's/:/%3A/g; s,/,%2F,g')
  url="https://www.instagram.com/oauth/authorize?client_id=$app_id&redirect_uri=$encoded&response_type=code&scope=instagram_business_basic%2Cinstagram_business_content_publish"
  note "④ ブラウザで次のページを開き（自動で開く）、「許可」を押す。"
  note "   $url"
  if command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true; fi
  note "   許可すると「このサイトにアクセスできません」と出る。それで正しい。"
  note "   そのときのアドレスバーの中身（$redirect?code=… で始まる）を全部コピーして、ここに貼り付ける。"
  note "   Instagram のホームが出てしまうなら、上の URL を Safari など別のブラウザに貼って開く。"
  read -r -p "  アドレスバーの中身: " pasted || true
  case "$pasted" in
    *error=*) die "許可されなかった： $(printf '%s' "$pasted" | sed -n 's/.*error_description=\([^&#]*\).*/\1/p' | tr '+' ' ')" ;;
  esac
  code=$(printf '%s' "$pasted" | sed -n 's/.*[?&]code=\([^&#]*\).*/\1/p')
  [ -n "$code" ] || die "貼り付けた中に code= が無い。アドレスバーの中身を最初から最後までコピーする"

  note "トークンに引き換える"
  # 値はコマンドの引数に出さず、curl の設定として標準入力から渡す
  reply=$(printf 'form-string = "client_id=%s"\nform-string = "client_secret=%s"\nform-string = "grant_type=authorization_code"\nform-string = "redirect_uri=%s"\nform-string = "code=%s"\n' \
            "$app_id" "$secret" "$redirect" "$code" \
          | curl -sS -K - https://api.instagram.com/oauth/access_token 2>&1) || die "Instagram につながらなかった： $reply"
  short=$(printf '%s' "$reply" | json_field access_token)
  if [ -z "$short" ]; then
    show_error "$reply"
    die "トークンに引き換えられなかった。code は1回しか使えないので、もう一度  bash setup.sh token  でやり直す"
  fi
  case "$reply" in
    *instagram_business_content_publish*) ;;
    *) note "注意：投稿の権限（instagram_business_content_publish）が入っていないように見える。確認（check）で分かる" ;;
  esac

  reply=$(printf 'url = "https://graph.instagram.com/access_token"\nget\ndata = "grant_type=ig_exchange_token"\ndata-urlencode = "client_secret=%s"\ndata-urlencode = "access_token=%s"\n' \
            "$secret" "$short" \
          | curl -sS -K - 2>&1) || die "Instagram につながらなかった： $reply"
  long=$(printf '%s' "$reply" | json_field access_token)
  secret=""
  if [ -z "$long" ]; then
    show_error "$reply"
    die "60日有効のトークンに引き換えられなかった"
  fi
  printf '%s' "$long" | gh secret set IG_ACCESS_TOKEN --repo "$REPO" >/dev/null || die "トークンを登録できなかった"
  short=""; long=""; reply=""
  note "60日有効のトークンを作って、そのまま GitHub に渡した"
}

# ---------------------------------------------------------------- 5. Drive の共有

step_share() {
  say "5/5  Drive のフォルダを、サービスアカウントに共有する"
  if [ -z "$SA_EMAIL" ]; then
    note "サービスアカウントのアドレスが分からない。鍵ファイルの client_email を見る"
  else
    note "アドレス： $SA_EMAIL"
  fi
  note "原稿と素材が入っている一番上のフォルダを、このアドレスに「編集者」で共有する。"
  note "（Claude に「共有して」と伝えれば、代わりにやってもらえる）"
  read -r -p "  共有が済んだら Enter " _ || true
}

# ---------------------------------------------------------------- 実行して結果を見る

run_mode() {
  local mode="$1" before id="" tries conclusion
  say "実行：$mode"
  before=$(gh run list --repo "$REPO" --workflow publish.yml --event workflow_dispatch --limit 1 \
             --json databaseId -q '.[0].databaseId // empty' 2>/dev/null || true)
  local started=""
  for tries in 1 2 3 4 5 6; do
    if gh workflow run publish.yml --repo "$REPO" -f mode="$mode" >/dev/null 2>&1; then started="yes"; break; fi
    sleep 5   # リポジトリを作った直後は、GitHub 側の準備が終わっていないことがある
  done
  [ -n "$started" ] || die "実行を始められなかった。GitHub の Actions タブを開き、publish が有効か確認する"
  for tries in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    sleep 3
    id=$(gh run list --repo "$REPO" --workflow publish.yml --event workflow_dispatch --limit 1 \
           --json databaseId -q '.[0].databaseId // empty' 2>/dev/null || true)
    if [ -n "$id" ] && [ "$id" != "$before" ]; then break; fi
    id=""
  done
  [ -n "$id" ] || die "始めた実行を見つけられなかった。GitHub の Actions タブで確認する"
  note "終わるまで待つ（1〜3分）…"
  gh run watch "$id" --repo "$REPO" --interval 5 >/dev/null 2>&1 || true
  conclusion=$(gh run view "$id" --repo "$REPO" --json conclusion -q .conclusion 2>/dev/null || echo "")

  # 本体が出した行だけを抜き出す（先頭の時刻と、設定の表示は落とす）
  local shown url
  url=$(gh run view "$id" --repo "$REPO" --json url -q .url 2>/dev/null || true)
  shown=$(gh run view "$id" --repo "$REPO" --log 2>/dev/null | awk -F'\t' '
    { line = $NF; sub(/^[^ ]+ /, "", line) }
    line ~ /^##\[group\]Run python -m autopost/ { inside = 1; body = 0; next }
    inside && !body { if (line ~ /^##\[endgroup\]/) body = 1; next }
    inside && line ~ /^##\[group\]/ { inside = 0 }
    inside && line ~ /^##\[error\]Process completed/ { next }
    inside { print "  " line }' || true)
  if [ -n "$shown" ]; then
    printf '%s\n' "$shown"
  else
    note "結果の行を取り出せなかった。ここを開いて「Run」を見る： $url"
  fi

  if [ "$mode" = "dry-run" ]; then
    rm -rf preview_out
    if gh run download "$id" --repo "$REPO" -n preview -D preview_out >/dev/null 2>&1; then
      note "切り抜き後の写真とキャプションを preview_out に置いた"
      if command -v open >/dev/null 2>&1; then open preview_out; fi
    fi
  fi

  if [ "$conclusion" = "success" ]; then
    note "→ 成功"
    return 0
  fi
  note "→ 失敗（$conclusion）。上の ✗ の行を見る。分からなければ、その行を Claude にそのまま貼る"
  note "   $url"
  return 1
}

# ---------------------------------------------------------------- 入口

case "${1:-setup}" in
  setup)
    preflight
    step_repo
    step_google
    step_vars
    step_token
    step_share
    run_mode check || die "接続の確認で失敗した。直してから  bash setup.sh check"
    run_mode dry-run || true
    say "ここまでで設定は終わり"
    note "preview_out の写真と caption.txt（改行が崩れていないか）を見る。"
    note "投稿する：  bash setup.sh post    （原稿の「日時」を過ぎてから。前だと「予約済み」と出るだけ）"
    note "定期実行を始める：  gh variable set AUTOPOST_ENABLED --repo $REPO --body true"
    ;;
  check|dry-run)
    preflight; find_repo
    run_mode "$1"
    ;;
  token)
    preflight; find_repo
    say "Instagram のトークンを作り直す"
    make_token
    note "✓ 登録した。確かめる：  bash setup.sh check"
    ;;
  post)
    preflight; find_repo
    say "Instagram に実際に投稿する"
    note "承認済みで、日時を過ぎている原稿がすべて対象になる。"
    ask "投稿してよいですか？" || die "中止した"
    run_mode run
    ;;
  *)
    sed -n '2,13p' "$(basename "$0")"; exit 1
    ;;
esac
