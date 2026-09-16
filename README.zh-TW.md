# doa6lr_ai — DOA6 Last Round 自動 Hold／反擊機器人

[English](README.md) | 繁體中文

An auto-hold / anti-CPU bot for **Dead or Alive 6 Last Round** (`DOA6LR.exe`, 64-bit, Windows).
It reads the opponent's move state straight out of game memory, picks the counter hold,
and sends keyboard input inside the startup window. Offline use only.

讀取遊戲記憶體裡對手的招式狀態（招式 ID、階段、幀數、打擊屬性、投技指令），在對手的發生幀窗內送出對應的 Hold，
投技則用蹲／後撤／側移／解投回應，並用學到的連段反擊。**只用於離線對 CPU**（Versus、訓練、連戰、街機）。

> 線上對真人開這個就是作弊。請不要。

---

## 成績

![DOA6LR True Fighter 最高分數榜，前七名都是機器人用不知火舞與女天狗打出來的](docs/true_fighter_record.png)

遊戲內 **True Fighter**（連戰）最高分數榜。前七名全是機器人打的：第 1、3 名用不知火舞（5,977,400／3,822,600），
第 2 名和第 4–7 名用女天狗（5,090,800 到 886,200）。第 8–10 名是沒動過的預設值。

## 現況

- 一般輸入延遲下 Hold 命中率 90–100%；連戰模式對 CPU 曾打到 155–2、62–1。
- 對手每一招的發生幀、投技類型、攻擊性投技（OH）、不能 Hold 的招、每個投技最好的回應方式，全部在對局中**自動學習並存成 JSON**，下次啟動直接沿用。
- 連段以「淨傷害 = 打出 − 被打」自動比較（多臂吃角子老虎），每個自己的角色分開統計。
- 對手換人（連戰模式）、自己坐 P1 或 P2、遊戲設 3-way 或 4-way Hold、刻意加的輸入延遲，都會自動偵測或用旗標對應。
- **目前只用女天狗（id 21）和不知火舞（id 30）訓練過。** 進攻表（戳擊、連段池）只有這兩隻，連段統計也只有這兩隻的資料；其他角色會退回通用連段池與 P 戳擊，要從零重新累積。對手方面，`startup.json` 等表只涵蓋實際遇過的 CPU 角色，沒遇過的角色第一次見到每一招都要先學（沒學過的招預設防禦）。

## 需求

- Windows 10/11，DOA6 Last Round（Steam）
- Python 3.10 以上。`holdbot.py` 只用標準函式庫（`ctypes` 讀記憶體、`SendInput` 送鍵盤）
- 掃描工具（`autoscan.py`、`probe.py`、`timeline.py`）另外需要 `numpy`
- 可選：`pip install vgamepad`（虛擬 Xbox 手把，需要 ViGEmBus）
- 以**系統管理員**身分執行終端機（`ReadProcessMemory` 需要）

遊戲內設定：鍵盤預設鍵位（H = J、P = K、K = L、T = M；U = P+K、I = S、O = H+K）。
訓練模式的 Hold 設定要跟 `--hold-mode` 一致。

## 快速開始

```powershell
git clone https://github.com/yuchinchenTW/doa6lr_ai.git
cd doa6lr_ai
python fields.py                      # 確認位址表還有效（應該印出雙方血量等欄位）
python holdbot.py --dry-run           # 只判斷、只印，不送輸入
python holdbot.py --hold-mode 4way    # 真的打（遊戲設 4-way 時）
python holdbot.py                     # 預設 3-way
```

啟動後先進對局（或訓練模式），程式會自己找座標列、確認自己是 P1 還是 P2，然後開始。`Ctrl-C` 結束並印統計。

## 常用旗標

| 旗標 | 預設 | 說明 |
|---|---|---|
| `--hold-mode 3way\|4way` | `3way` | 要跟遊戲設定一致；3-way 時中腳也用 4H |
| `--me auto\|P1\|P2` | `auto` | 自動偵測鍵盤控制的是哪一邊 |
| `--window N` | 16 | 對手剩幾幀進判定時出 Hold |
| `--poke ...` | `auto` | 對手閒置時的戳擊，依角色自動選（女天狗 P+K、不知火舞 4P） |
| `--combo ...` | `auto` | 打中後的連段；`auto` 用角色連段池自動比較 |
| `--no-hold-in-stun` | 關 | 預設在 hit stun 中也出 Hold（遊戲會暫存輸入到硬直結束） |
| `--no-break-blow` | 關 | 預設 Break Gauge 滿時用 6S Break Blow 反擊 |
| `--break-hold` | 關 | 開啟 4S Break Hold |
| `--throw-answer crouch\|jab\|none` | `crouch` | 投技的基本回應；每個投技會再自動學最好的 |
| `--unknown guard\|skip\|hold` | `guard` | 沒學過發生幀的招怎麼處理 |
| `--dry-run` / `--probe` | | 不送輸入 / 只看偵測與學 startup |

完整清單：`python holdbot.py --help`。

## 它學什麼、存在哪

| 檔案 | 鍵 | 內容 |
|---|---|---|
| `startup.json` | 對手角色 + 招式 | 判定開始幀（看到 Phase 0→1 那一幀記下） |
| `throws.json` | 對手角色 + 招式 | 投技的 CommandCode 與上中下段 |
| `throw_answers.json` | 對手角色 + 招式 | 蹲／後撤／側移／防禦各自的成功次數；失敗兩次自動換 |
| `throw_escapes.json` | 投技的 CommandCode（跨角色共用） | 解投輸入（T／6T／4T／2T）各自的成功次數；先照 CommandCode 猜，失敗兩次自動換 |
| `oh.json` | 對手角色 | 攻擊性投技（會抓 Hold 的招） |
| `nohold.json` | 對手角色 | Hold 接到卻 0 傷害的招，改用防禦 |
| `stun_holds.json` | 我方受創動畫 id | 在該硬直中出 Hold 的成敗；0/3 或倒地動畫就退休 |
| `combo_stats.json` | 自己的角色 + 起手招 | 每條連段的次數與淨傷害 |
| `pos.json` | | 角色物件內座標欄位的偏移 |
| `layout.json` | | 記憶體位址表（靜態指標鏈 + 欄位偏移） |

自己換角色不會影響對手相關的學習；對手相關的表按對手角色 id 分開。

## 輸出怎麼讀

`#n` Hold、`~` 投技回應（`ours:` 是我方招式 ID 序列）、`*` 戳擊、`+` 連段、`=` 防禦、`>` 對助跑出手、
`<` 起身後撤、`^` 倒地起身、`!` 學到新東西或被抓、`(skip)` 看到但當下不能動、
`THROWN` 被投前 20 個狀態變化。結尾統計有各類命中率、被誰打掉多少血、當時剛做了什麼、連段排名、回合勝負。

## 專案結構

| 檔案 | 用途 |
|---|---|
| `holdbot.py` | 主程式：偵測 → 學習 → 面向探針 → Hold／防禦／投技回應／連段 |
| `fields.py` + `layout.json` | 解析指標鏈、讀欄位 |
| `pad.py` | 鍵盤／虛擬手把注入、Hold 對照表 |
| `memlib.py` | `ReadProcessMemory` 封裝、記憶體區段列舉 |
| `autoscan.py`、`probe.py`、`timeline.py`、`analyse2.py`、`pointerscan.py`、`scanner.py`、`watch.py`、`reanalyse.py` | 找欄位用的工具鏈；遊戲更新讓 `layout.json` 失效時重跑 |
| `docs/NOTES.md` | 開發過程的技術筆記：位址怎麼找到的、每個欄位的實測值、DOA6 輸入的坑、各版本實戰結果 |

## 已知限制

- 鍵盤 `SendInput` 送不出斜方向 + 按鈕（3P、1P 等），所以連段池只有按鈕與水平方向。
- 7 幀的突進投在 100 以內起手時沒有反應時間，只能靠先出拳。
- 面向沒有記憶體欄位，靠走路 ID 探測；換邊後第一次 Hold 若剩餘幀數太少可能鏡像。
- 位址表對應 2026-06 上市的 Last Round 版本；遊戲更新後要用工具鏈重找。

## 授權

MIT License，見 `LICENSE`。
