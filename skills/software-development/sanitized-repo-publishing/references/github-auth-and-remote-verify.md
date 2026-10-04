# GitHub 认证 → 建私有仓 → 推送 → 远端核对 → 转公开

全部变量先定好（**代理必须显式**；token 不进对话、不打进日志）：

```sh
PROXY=http://<宿主>:17890            # 端口别猜：echo > /dev/tcp/<宿主>/17890 试一下
CID=178c6fc778ccc68e1d6a             # GitHub 官方公开 client id（非密），设备码流程用它
TOKEN_FILE=~/.secrets/github.token   # 必须是自己**可写**的目录；secrets/ 常被 root 占住
```

## 1. 设备码换 token（agent 自己建仓用；推送不用它）

```sh
# ① 申请设备码
curl -sS -x $PROXY -A "Mianmian-Publisher/1.0" \
  --data "client_id=$CID&scope=repo" -H "Accept: application/json" \
  https://github.com/login/device/code
# → {"device_code":"…","user_code":"XXXX-XXXX","expires_in":900}
```

把 `user_code` 与 `https://github.com/login/device` 发给主人，**并说明**「页面显示授权给 GitHub CLI 是正常的」。
设备码常在**十几分钟后过期**，过期就重新申请一次、把新码发过去。

```sh
# ② 后台轮询（拿到 token 就落盘）
#    首次请求 ① 得到的 device_code；轮询 body：
#    client_id=$CID&device_code=$DEV&grant_type=urn:ietf:params:oauth:grant-type:device_code
#    返回里出现 access_token → 写文件；authorization_pending → 继续；slow_down → 间隔 +5s
#    expired_token → 让主人重新授权（重新申请设备码）
```

**脚本纪律**（踩过）：写盘步骤要 `|| { echo WRITE_FAILED; exit 1; }`，**不能先写后无脑打印 LOGIN_OK**；
拿到 token 后**回头 `test -s $TOKEN_FILE` 验一次**再往下走。

## 2. 建私有空仓（push 不能建仓）

```sh
TOK=$(cat $TOKEN_FILE)
curl -sS -x $PROXY -A "Mianmian-Publisher/1.0" -H "Authorization: token $TOK" \
  -H "Accept: application/vnd.github+json" -d '{"name":"<repo>","private":true,"auto_init":false}' \
  https://api.github.com/user/repos
# auto_init=false：不要 README/.gitignore/LICENSE，否则推送时要先 merge，白折腾一轮
```

> `-H "Authorization: token $TOK"` **原样写在命令行**里。写成 `A="-x $PX -H Authorization: token $TOK"`
> 再 `curl $A` 会被 word-splitting 拆坏 header、把 token 当 URL，静默变成 401/502。

## 3. 推送走 SSH（token 只留给 API）

```sh
# ~/.ssh/config（幂等追加一次）
Host github.com
    HostName github.com
    User git
    IdentityFile <持久挂载里的私钥，如 /opt/data/.ssh/id_ed25519>
    StrictHostKeyChecking accept-new

git remote add origin git@github.com:<user>/<repo>.git
git branch -M main          # 远端默认分支是 main，别推成 master
scripts/publish_push.sh "说明"
```

远端空仓首次推送要 `git push -u origin main`；本地提交成功但推送失败**不要回滚提交**，修好网络重跑同一条命令。

## 4. 远端读回核对（必做，别只看本地）

```sh
B=https://api.github.com/repos/<user>/<repo>
git ls-remote origin                                   # ① commit 对不对
curl -sS -x $PROXY -A x -H "Authorization: token $(cat $TOKEN_FILE)" \
  "$B/git/trees/main?recursive=1"                      # ② 文件数 + 敏感路径（persona/memories/logs/token…）
curl -sS -x $PROXY -A x -H "Authorization: token $(cat $TOKEN_FILE)" \
  "$B/contents/<path>?ref=main"                        # ③ 抽文件读**内容**：decode base64 后搜真实号码/占位符
```

核对点：文件数与产物一致；敏感路径 0；抽检文件里真实凭证/号码 0 处、占位符在位。

## 5. 转公开 + 验证公开

```sh
curl -sS -x $PROXY -A x -H "Authorization: token $(cat $TOKEN_FILE)" -X PATCH \
  -d '{"private":false}' $B          # private=false
# 匿名验证（不带任何凭证）：仓库页 HTML 与 raw 文件都要 200
curl -s -o /dev/null -w '%{http_code}\n' https://github.com/<user>/<repo>
curl -sS -x $PROXY -o /dev/null -w '%{http_code}\n' \
  https://raw.githubusercontent.com/<user>/<repo>/main/README.md
```

`raw.githubusercontent.com` 直连常不通，**挂代理**再验（仍然是匿名请求，不算「用凭证自证」）。

## 6. 留给主人的后路

token 只存在本机可写目录（600，不在发布白名单里）；告诉主人撤销路径：
GitHub → Settings → Applications → GitHub CLI → Revoke——撤销只影响建仓/查询，推送走 SSH 不受影响。
