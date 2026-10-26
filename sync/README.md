# 노트북 → 메인 PC 자동 동기화 (Tailscale + Syncthing)

노트북에서 녹음·촬영한 파일이 메인 PC로 **자동 전송**되도록 하는 설정 방법입니다.

```
[노트북]  소리 녹음 / 동영상 폴더  ──(보내기 전용)──▶  [메인 PC]  D:\강의자료\소리 녹음 / 동영상
                         Syncthing 이 파일 전송, Tailscale 이 두 PC를 사설망으로 연결
```

| 구성 요소 | 역할 |
|---|---|
| **Tailscale** | 집·학교 등 다른 네트워크에 있어도 두 PC를 같은 사설망(100.x.x.x)으로 연결 |
| **Syncthing** | 폴더를 감시하다가 바뀐 파일을 상대 PC로 전송 (끊겨도 이어서 전송) |
| `setup_sync.ps1` | 위 두 가지 설치·자동 시작·폴더 공유 설정을 한 번에 처리 |
| `sync_config.json` | 동기화할 폴더 목록과 메인 PC 저장 위치 |

## 동작 방식

- **단방향**: 노트북 → 메인 PC로만 전송합니다. 메인 PC에서 파일을 고치거나 지워도 노트북에는 영향이 없습니다.
- **노트북에서 지워도 메인 PC 사본은 남습니다** (`keep_deleted_on_main: true`). 노트북 용량을 비워도 원본이 보존됩니다.
- **자동 시작**: 두 PC 모두 Windows 로그온 시 Syncthing이 백그라운드에서 자동 실행됩니다 (작업 스케줄러 `Syncthing (강의 동기화)`).
- **전송 시점**: 파일이 바뀌고 약 1분 뒤 전송. 두 PC가 **동시에 켜져 있고 인터넷에 연결**되어 있어야 합니다.
- **보안**: 공용 릴레이·인터넷 탐색을 끄고 Tailscale 주소로만 통신하므로 파일이 tailnet 밖으로 나가지 않습니다.

---

## 0. 준비

1. 두 PC 모두 Windows 10/11, `winget` 사용 가능 (Microsoft Store의 "앱 설치 관리자")
2. **Tailscale 계정 1개**. 두 PC가 반드시 같은 계정으로 로그인해야 합니다.
3. 이 `sync` 폴더를 **두 PC 모두에** 둡니다 (git clone, USB 복사 등).
4. `sync_config.json` 확인·수정

```json
{
  "folders": [
    {"id": "recordings", "label": "소리 녹음", "laptop_path": "C:\\Users\\U301-8\\OneDrive\\문서\\소리 녹음"},
    {"id": "videos",     "label": "동영상",   "laptop_path": ""}
  ],
  "main_base_dir": "D:\\강의자료",
  "keep_deleted_on_main": true
}
```

| 항목 | 설명 |
|---|---|
| `laptop_path` | **노트북**의 원본 폴더 경로. 비워 두면 그 폴더는 건너뜀 → 동영상 폴더는 여기에 입력 |
| `main_base_dir` | **메인 PC**에서 받을 위치. 그 아래에 `label` 이름으로 폴더 생성. 메인 PC에 D 드라이브가 없으면 수정 |
| `keep_deleted_on_main` | `true`면 노트북에서 지운 파일도 메인 PC에 유지 |

> ⚠ JSON에서 경로의 `\`는 `\\`로 두 번 적어야 합니다.
> ⚠ `main_base_dir`은 **OneDrive 폴더 밖**으로 두세요. OneDrive와 Syncthing이 같은 파일을 동시에 건드리면 충돌 사본이 생길 수 있습니다.
> 두 PC의 `sync_config.json` 내용은 같아야 합니다 (`id`가 서로 짝을 맞추는 기준).

---

## 1. 설정 순서 (총 3번 실행)

두 PC가 서로의 **기기 ID**를 알아야 연결되므로 메인 → 노트북 → 메인 순으로 실행합니다.
모든 명령은 `sync` 폴더에서 PowerShell로 실행합니다.

### ① 메인 PC: 설치 + 메인 기기 ID 확인

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_sync.ps1 -Role Main
```

- 끝나면 화면에 이렇게 나옵니다. **ID를 복사해 두세요.**
  ```
  ==================================================
   내 기기 ID (Main): ABCDEFG-HIJKLMN-...
  ==================================================
  ```
- 메인 PC의 **Tailscale 이름**도 확인합니다.
  ```powershell
  tailscale status
  ```
  첫 줄의 `100.x.x.x  <이름>` 중 이름 또는 IP를 사용합니다.

### ② 노트북: 설치 + 메인 PC 연결

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_sync.ps1 -Role Laptop -PeerId <메인 기기 ID> -PeerHost <메인 Tailscale 이름 또는 IP>
```

- Tailscale이 없으면 설치합니다 → **관리자 승인(UAC) 창에서 "예"**.
- 로그인이 필요하면 브라우저가 열립니다 → **메인 PC와 같은 Tailscale 계정**으로 로그인.
- 끝나면 노트북의 **기기 ID**가 출력됩니다 → 복사.
- 노트북 Tailscale 이름도 `tailscale status`로 확인.

### ③ 메인 PC: 노트북 연결

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_sync.ps1 -Role Main -PeerId <노트북 기기 ID> -PeerHost <노트북 Tailscale 이름 또는 IP>
```

`공유 설정: 소리 녹음 [receiveonly] D:\강의자료\소리 녹음` 이 나오면 설정 완료입니다.

---

## 2. 동작 확인

1. 두 PC에서 브라우저로 **http://127.0.0.1:8384** 접속 (Syncthing 관리 화면)
2. 오른쪽 "원격 기기"에 상대 PC가 **연결됨**으로 표시되는지 확인
3. 왼쪽 폴더가 **최신 상태**인지 확인 (처음에는 기존 파일 전송 때문에 시간이 걸림)
4. 노트북에서 짧게 녹음 → 1~2분 뒤 메인 PC의 `D:\강의자료\소리 녹음`에 파일이 생기면 성공

---

## 3. 나중에 바꿀 때

| 하고 싶은 것 | 방법 |
|---|---|
| 동영상 폴더 추가 | 두 PC의 `sync_config.json`에서 `videos`의 `laptop_path` 입력 → ②, ③ 다시 실행 |
| 폴더 추가 | `folders`에 `{"id": "고유영문id", "label": "폴더이름", "laptop_path": "..."}` 추가 → ②, ③ 다시 실행 |
| 메인 PC 저장 위치 변경 | `main_base_dir` 수정 → ③ 다시 실행 (기존 받은 파일은 수동으로 옮기기) |
| Syncthing 업데이트 후 | `winget upgrade` 후 경로가 바뀌어 자동 시작이 안 될 수 있음 → 해당 PC에서 스크립트 다시 실행 |

스크립트는 여러 번 실행해도 안전합니다 (설정을 덮어씀).

### 강의 변환 파이프라인을 메인 PC에서 돌릴 경우

메인 PC의 `config.json`에서 `source_dir`을 받은 폴더로 바꿉니다.

```json
"source_dir": "D:\\강의자료\\소리 녹음"
```

녹음 중에 전송된 파일은 계속 바뀌므로, 파이프라인의 `skip_if_modified_within_sec`(기본 120초) 대기가 필요합니다. 줄이지 마세요.

---

## 4. 문제 해결

| 증상 | 원인·해결 |
|---|---|
| `스크립트를 실행할 수 없습니다` (실행 정책) | 위 명령처럼 `powershell -ExecutionPolicy Bypass -File ...` 형태로 실행 |
| 한글이 깨져 보임 | `setup_sync.ps1`이 UTF-8(BOM) 형식이어야 함. 편집기에서 저장 형식을 바꿨다면 "UTF-8 with BOM"으로 다시 저장 |
| 상대 기기가 계속 **연결 끊김** | ① 두 PC가 같은 Tailscale 계정인지 (`tailscale status`에 서로 보이는지) ② `ping <상대 Tailscale IP>` 확인 ③ **Windows 방화벽**: Syncthing 첫 실행 시 뜬 방화벽 창에서 차단했다면, "Windows Defender 방화벽 → 앱 허용"에서 syncthing을 **개인·공용 모두 허용** |
| `Syncthing API에 연결하지 못했습니다` | 작업 관리자에서 syncthing 실행 여부 확인. 없으면 작업 스케줄러에서 `Syncthing (강의 동기화)` 수동 실행 후 스크립트 재실행 |
| `폴더 없음: ...` | 노트북의 `laptop_path` 경로 오타. 탐색기 주소창에서 경로를 복사해 `\`를 `\\`로 바꿔 입력 |
| 전송이 매우 느림 | 학교 등에서 Tailscale 직접 연결이 막혀 중계(DERP)를 거치는 경우. `tailscale status`에서 상대 줄에 `relay`가 보이면 해당. 집 네트워크에서 동기화되도록 두는 것이 현실적 |
| 메인 PC 폴더에 "로컬 변경" 경고 | 받기 전용 폴더에서 파일을 직접 고친 경우. 관리 화면의 **로컬 변경 되돌리기**를 누르거나, 작업은 사본으로 할 것 |

---

## 5. 끄기 / 제거

```powershell
# 자동 시작 해제 + 중지
Unregister-ScheduledTask -TaskName "Syncthing (강의 동기화)" -Confirm:$false
Stop-Process -Name syncthing

# 프로그램 제거 (선택)
winget uninstall Syncthing.Syncthing
winget uninstall Tailscale.Tailscale
```

설정은 `%LOCALAPPDATA%\Syncthing`에 남아 있으니 완전히 지우려면 이 폴더도 삭제합니다. 동기화된 파일(메인 PC의 `D:\강의자료`)은 지워지지 않습니다.

---

## 알려진 한계

- **두 PC가 동시에 켜져 있어야** 전송됩니다. 노트북만 켜진 동안 녹음한 파일은 메인 PC가 켜질 때 전송됩니다.
- 녹음 중인 파일도 1분 간격으로 바뀐 부분이 전송되고, 최종본은 녹음이 끝난 뒤 맞춰집니다.
- 노트북 녹음 폴더 안에 Syncthing 표시용 빈 폴더 `.stfolder`가 생깁니다 (지우면 동기화가 멈춤). OneDrive에도 같이 올라가지만 무해합니다.
- 메인 PC에서 받은 파일을 지우거나 고치면 "로컬 변경"으로 표시되고 노트북에는 반영되지 않습니다. 관리 화면의 **로컬 변경 되돌리기**를 누르면 노트북에 원본이 남아 있는 파일은 다시 받아 옵니다.
- 아직 실제 두 PC 사이에서 전송을 검증하지 않았습니다. 처음 설정 후 「2. 동작 확인」을 꼭 거치세요.
