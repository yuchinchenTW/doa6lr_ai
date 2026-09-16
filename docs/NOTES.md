# DOA6LR auto-hold — 技術筆記

DOA5LR 那套自動格擋（doa5ai）的 DOA6 Last Round 版。這份是開發過程的技術筆記；安裝與用法看上一層的 `README.zh-TW.md`（英文版 `README.md`）。
目標一樣：讀對手的攻擊屬性，選出剋它的 Hold，在發生幀窗內送出輸入。

> 離線 / 訓練模式 / 本機對戰。線上排位開這個就是對真人作弊。

---

## 現況（2026-09-14 晚）

**位址表已經有了**（`layout.json`），是當天從零找出來的，不是借來的。

| | DOA5LR | DOA6LR |
|---|---|---|
| 執行檔 | `game.exe`，32-bit | `DOA6LR.exe`，64-bit（2026-06-25 上市） |
| 位址來源 | WAZAAAAA Toolbox，AOB 錨點 `AF 47 E9 42` | 自己找的兩條靜態指標鏈（見下） |
| 角色資料 | 一個角色散在多個物件 | 每個角色一塊 1.1 MB，P1／P2 各自一條指標鏈（兩塊的距離換角色就變，不能用固定 stride） |
| Hold 對照 | 7H / 4H / 6H / 1H | 遊戲有 **3-way / 4-way** 設定：3-way 時 4H 包中拳中腳、6H 只是防禦；4-way 同 DOA5。`--hold-mode` 要跟遊戲設定一致 |
| 預設鍵盤 | K/L/J/M | **相同** |
| 發生幀總數 | 遊戲直接給 `TotalStartup` | **沒有這個欄位**，要從階段切換學 |

前人作品查過：DOA5 的特徵碼在 DOA6LR 零命中；網路上所有 DOA6 Cheat Table 都是 2019 年 `DOA6.exe` 的靜態位址，
只有血量／解鎖，Last Round 是新建置不適用；WAZAAAAA 沒做 DOA6 版。

## 找到的欄位

錨點（寫在 `layout.json`，`fields.py` 每次啟動解析）：

```
state : [DOA6LR.exe+0x7F55158] = R (P2 player 物件)
        [R+0x8] = S (P1 player 物件)
        [S+0x3A8] = Q,  Q+0x30 = P1 狀態物件
        [R+0x3A8] = Q2, Q2+0x30 = P2 狀態物件     （曾經是 P1+0x10DFA0，換角色後變成 P1-0x2FBA0，所以各走各的鏈）
pos   : [DOA6LR.exe+0x5DAFD10] = 座標物件         P1 vec4 在 +0x5B0，P2 在 +0x610
```

狀態物件內的 offset（P1、P2 相同）：

| 欄位 | offset | 型別 | 實測值 |
|---|---|---|---|
| `CurrentCharacter` | `+0x14` | u32 | 21 / 5 |
| `CurrentMove` | `+0x68` | u16 | 0 待機；角色 21 的 P=176、K=179、2K=181；被打反應 24169 等 |
| `CommandCode` | `+0x100` | u16 | P=1000、K=1100、2K=1120、6P=1060、投=363、Hold=168、防禦=284；會殘留，CPU 的一招讀到 254，不能當閘門 |
| **`Phase`** | `+0x128` | u16 | **0 發生、1 判定、2 收招**。待機也是 0，要配 `MoveKind` |
| `CurrentMoveFrame` | `+0x174` | u16 | 招式第 1 幀 = 1，每幀 +1；待機會飽和到 65535 |
| `InStartup` | `+0x1A4` | u8 | 錄製時只在發生幀為 0，但另一場待機也讀到 0，**不可靠** |
| **`MoveKind`** | `+0x108` | u8 | **3 打擊、16 投技、5 Hold、0 待機／防禦／移動**（等同 DOA5 的 MoveType，這才是攻擊閘門） |
| `MoveType` | `+0x578` | u8 | 2 待機／移動、1 做任何指令動作（打擊、投、Hold、防禦、側移）、3 被打中、5 投技命中 |
| **`CurrentHealth`** | `+0x580` | u16 | 滿 300；P 打 14、K 打 16；訓練模式約 1.5 秒回滿 |
| `AnimLength` | `+0x630` | u32 | 動畫總長；待機 720 |
| `HighMidLowGround` | `+0x8EC` | u32 | 1 上、2 中、3 下（會殘留） |
| **`StrikeType`** | `+0x904` | u8 | **跟 DOA5 同一組枚舉**：0 上拳 1 上腳 2 中拳 3 中腳 4 下拳 5 下腳（會殘留） |
| `XAxis/YAxis/ZAxis` | pos `+0x5B0..` | f32 | 單位約公分，訓練模式起手距離約 190 |

實測角色 21 的判定開始幀：P 第 15 幀、K 與 6P 第 16 幀、2K 第 18 幀（`CurrentMoveFrame` 讀值）。
四種 Hold 的招式 ID：7H=152、4H=154、6H=155、1H=156，判定窗約按下後第 4 到 22 幀。

**沒找到的：** `TotalStartup`（整塊 1.1 MB 裡沒有任何欄位等於它）、面向欄位（目前用兩人 X 座標算）、背對狀態。

## 用法

```powershell
cd doa6lr_ai
python fields.py                 # 解析錨點、印所有欄位（P1 血量 300 = layout 還有效）
python fields.py --watch         # 即時看
python holdbot.py --probe        # 只看偵測；對手出招時印階段/幀數，並順便學 startup
python holdbot.py --dry-run      # 判斷並記錄，不送輸入
python holdbot.py                # 真的送
```

### startup 是學來的

`holdbot` 看到對手 `Phase` 從 0 變 1 的那一幀，就把 `CurrentMoveFrame` 記進 `startup.json`（鍵是角色 ID + 招式 ID）。
沒學過的招預設**防禦**（`--unknown guard`），第二次來就知道還剩幾幀。訓練模式假人重複出招，所以很快就學齊。

`--probe` 模式也會學，可以先讓假人循環出招跑一分鐘，再開真的。

### 其他旗標

跟 DOA5 版一樣：`--window 16`、`--min-remaining 4`、`--guard-types 3,5`、`--hold midk=4`、`--pretap`、`--press`。
新的：`--hold-mode 3way|4way`（對應訓練模式的 Hold 設定，預設 3way）、`--no-hold-in-stun`（預設在 hit stun 中也會出 Hold：受創中只有 Hold 這個輸入有效，每個受創動畫 id 的成功／嘗試次數記在 `stun_holds.json`，某個 id 試 3 次都沒反應就不再在那裡出手；`--stun-hold-frame 4` 為受創動畫至少跑幾幀才出手）、`--no-punish-throws`（預設對手 `MoveKind == 16` 投技發生時出拳反制）、`--distance 250`（公分）。

## 實戰結果與行為（2026-09-15，角色 21 vs CPU 角色 21，3-way Hold）

**最近兩場完整對局：5–4、5–0。** Hold 40/51（78%）、25/28（89%）；下段 Hold 23/25，中腳 12/12，助跑出拳 18/18。
輸血的來源全是投技，所以 holdbot 長出一整套進攻與距離管理，每一條都有 THROWN dump 當證據：

| 情境（對手動作） | 我方反應 | 依據 |
|---|---|---|
| 打擊技起手 | Hold（7H／4H／1H；3-way 中腳也 4H） | 75–100% |
| Hold 變成防禦（270／271） | H 按住到攻擊結束，270 順便翻面向 | 3-way 反向 4H = 6H |
| 對手閒置在 60–130 | **先出拳**（P，15 幀）— 7 幀突進投 8340 唯一的剋星 | 31 戳 20 中 |
| 戳中後對手硬直（kind 8） | 追一拳 | — |
| 助跑 188／8015（kind 2，從不進判定） | 200 內出拳；188 第 18 幀出腳當誘餌（8149 突進撞上） | 18/18、誘餌 45 血 ×4 |
| 標準投 T（CommandCode 363） | 站著不動；被抓瞬間按 T 脫逃 | 脫逃 4/4（手冊：只有 T 可脫） |
| 下段投（h/m/l 3） | 站著不動，抓空後 punish | — |
| 方向投／跑投（8144／8148／8284／8340，h/m/l 1） | **斜下蹲**（後+下，id 8/9→13）；抓空後 punish | 8340 抓空 20+ 次、punish 13/13；T 脫逃 0/22 |
| 投技 ≤ 130、剩 ≥ 11 幀、我方站定 | 出拳 | — |
| 對手倒地／起身，距離 ≤ 200 | 後撤到 220 外 | — |
| 我方倒地（ID 125–135） | 0.1 秒後點 H 起身 | — |
| 雙方閒置 < 180 | 走到 220 外；靠牆就改戳 | — |
| 開場動畫（17）中對手起跑投 | 直接進投技對策（17 期間輸入有效） | — |
| 開場動畫結束、對手閒置 | 先出拳 | 45 血 ×3 |

**位置區塊的 P2 偏移不是固定的**：P1 的座標在 pos+0x5B0，P2 在角色 21 對 21/35/31/30 時是 +0x610（stride 0x60），但角色 20 對角色 6 時跑到 +0x6D0（stride 0x120），舊偏移讀到殘留值、距離變 9984、所有 hold 因為距離門檻不觸發。啟動時依「vec4(w=1) ＋ 純量列」的列型態自動找 P2 的列（印 `position rows:`），比賽中距離不合理時會重找。

**Offensive Hold（OH）**：有些被 StrikeType 標成打擊的招其實是投技，Hold 出去會被它抓（角色 31 的 8009：三次中P hold 全變成被投，103 血）。我方 hold 按下後 0.35 秒內變成 MoveType 5，就把那招記進 `oh.json`（按角色），之後看到它起手改側移。

**蹲的正確按法**：DOA6 單按 ↓ 是**側移**（ID 31／32），跑投會追側移，之前 42 次「蹲」全部被抓就是這個原因。真正的蹲是 ↓+← 斜下（ID 8／9 → 13），7 幀的衝刺投 8340 從 136–240 起手每次都抓空。投技分類靠對手起手時的 `CommandCode`（T=363、8340=364）與 `HighMidLowGround`（3＝下段），每個投技 ID 記在 `throws.json`。

**Break Gauge 找到了：`+0x5B4` u16，0–200，200 = 滿條**，開場 100，跨回合累積，Break Hold 花約 100。實戰對照：我方釘在 200 時畫面上的條是滿的、對手挨打時 104→147。以前用的 `+0x584` 只是累計傷害，不是量表。現在滿條時會用 **6S Break Blow**：擋下對手後的確定反擊、以及打中對手進入硬直時直接接 Break Blow（取代 Fatal Rush 字串）。Break Hold（4S）仍預設關（一般 hold 已 90%+，且方向錯時 4S 會變 6S）。

**不知火舞（角色 30）**：戳擊用 **4P**（15 幀上段，普通命中 +35 硬直；她的 P 9 幀最快但命中只有 −2），連段候選 `S,S,S,S`／`P,P,P,P`／`P,P,K`／`K,K,K`／`6P,K,K`／`P,P,2K`／`S`／`P`，同樣用淨傷害自動比較。她的浮かせ（3P+K、8K、3P）都要斜方向或上，鍵盤送不出，所以不在池裡。資料來源：Free Step Dodge 幀數表與新手指南、doa6.seesaa.net。其他沒建表的角色用通用池 `P,P,K`／`P,P,P`／`S,S,S,S`／`S`／`P`、戳擊 P。

**判斷是否能行動**：`MoveType == 1` 且有招式 ID＝忙碌；`MoveType 3／5`、ID 24000–26999＝被打／被投；ID 125–135（type 2）＝倒地、7x（type 1）＝起身；狀態 17＝開場（投技例外）。硬直（hit stun）中的 Hold 有效：輸入會被遊戲暫存、在硬直結束那一幀執行（2026-09-16 實測 25861 4/4、24001 8/13），每個受創動畫 id 的成敗記在 `stun_holds.json`。

**面向**：沒有欄位。每次 Hold 先按水平方向，等走路 ID（1／3 前、2／4 後）——它要 ~42 ms 才出現，探針等 75 ms（剩餘 <9 幀時 45 ms）；按下前已經在顯示的走路 ID 是上一段走路的尾巴，不算。反了就翻轉重按。閒置時每 1.5 秒與每次恢復行動時點右鍵校正；拉距離時走路成前進也翻（120 ms 防抖，之前每 tick 翻一次）。從後退走路狀態出 Hold 只有 1/3（back 按著＝防禦，加 down+H 變 271），所以出 Hold 前先等走路 ID 消失（≤50 ms）。

**沒解的**：7 幀的 8340 在 100 內起手（只能靠先出拳）；連段中的 8171／8385 組合投；起身瞬間（type 13）被 Fatal Rush 8399 開頭打到。

**輸出怎麼讀**：`#n` Hold、`~` 投技對策（`ours:` 是我方 ID 序列）、`>` 助跑、`*` 戳、`<` 起身後撤、`^` 倒地起身、`=` 防禦、`(skip)` 看到但不能動、`THROWN` 被投前 20 個狀態變化（每個投技一份）、`ROUND WON／round lost`。結尾有各類命中率、我方被誰打掉多少血、當時剛做了什麼、回合勝負。

## 工具鏈（找欄位用，layout 失效時重跑）

```powershell
python autoscan.py all                       # 自動按鍵 + 差分掃描：幀計數器 / 座標 / 姿勢 / 血量
python pointerscan.py 0x<狀態物件位址>        # 找 DOA6LR.exe+靜態偏移 -> 指標鏈
python probe.py record --preset offense      # 一邊出招一邊 120Hz 整塊錄 P1/P2 物件 -> probe.npz
python probe.py record --preset defense --out probe_defense.npz   # 投技 / 四種 Hold / 防禦 / 側移
python probe.py analyse --obj A              # 哪些 offset 在每招內恆定、招與招之間不同
python timeline.py --pick 0x128:2,0x174:2 --phase   # 逐幀看 / 找階段型欄位
python analyse2.py                           # 找 startup、血量
```

`autoscan.py frame` 是整條鏈的起點：整個 8 GB 裡以 60Hz 精確前進、且出拳時歸零的欄位只有幾個，
從那裡往外找到 1.1 MB 的角色區塊，`pointerscan.py` 再找出靜態根。

## 檔案

| 檔案 | 用途 |
|---|---|
| `holdbot.py` | 主迴圈：偵測 → 學 startup → 面向探針 → Hold / 防禦 → `MoveType 6` 判定成功；失敗會印逐幀 TRACE |
| `fields.py` + `layout.json` | 錨點解析與欄位表（取代 DOA5 的 `aob.py`） |
| `startup.json` | 學到的 (角色, 招式) → 判定開始幀 |
| `exceptions.json` | 不要 Hold 的招式 |
| `pad.py` | 輸入注入、Hold 對照表（DOA6 規則相同，未改邏輯） |
| `autoscan.py` / `probe.py` / `timeline.py` / `analyse2.py` | 欄位獵手（見上） |
| `scanner.py` / `watch.py` / `pointerscan.py` / `reanalyse.py` / `memlib.py` | DOA5 帶來的通用工具，改成 64-bit |

## 掃描器在 DOA6LR 上的三個調整

**排除 PAGE_WRITECOMBINE。** 3.3 GB 的 GPU 上傳堆每幀都在變、不含遊戲狀態。

**分塊 + 跳過全零。** 4.2 GB 的 arena 只有約 1 GB 非零。快照從 8.5 GB 降到 2.9 GB，約 3 秒。

**每塊記時間戳。** `filter_rate` 用每個塊自己的間隔算期望值，「60Hz」才能開到 ±4 的容忍。
第一輪之後要立刻重讀一次候選（`refresh_last`），否則第二輪的基準值分散在 11 秒內，會把對的全砍掉 —— 第一次跑就踩到。

## 之前在 DOA5 學到、這次直接帶過來的

- `timeBeginPeriod(1)` 必開。
- Hold 是 `J`，不是 `L`。
- 對角 Hold（7、1）的鍵要在同一個 SendInput 呼叫送出。
- 防禦只按 Free，不能帶方向。
- `StrikeType` 會殘留上一招的值，一定要用 `MoveKind == 3 and Phase == 0` 當閘門。
- 每一個實測結論都要固定角色組合、CPU 行為、單一參數。
