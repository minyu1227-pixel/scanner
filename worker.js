/**
 * Cloudflare Worker — scanner 레포의 GitHub Actions(Scanner 워크플로)를 매일 정해진 시각에 실행
 *
 *  1차 (PRIMARY_CRON) : 무조건 workflow_dispatch 호출
 *  2차 (BACKUP_CRON)  : 오늘(한국시간) 이미 성공한 Scanner 실행이 있으면 건너뜀, 없으면 1차와 똑같이 실행
 *                       확인 과정 자체가 실패하면 안전하게 그냥 실행
 *  일요일(한국시간)    : 둘 다 건너뜀
 *
 *  GitHub 토큰은 코드에 쓰지 않고 Cloudflare Secret(GH_TOKEN)에서 읽습니다.
 *    등록: npx wrangler secret put GH_TOKEN
 */

const REPO = "minyu1227-pixel/scanner";
const WORKFLOW_FILE = "scan.yml";   // .github/workflows/scan.yml
const BRANCH = "main";

// cron은 UTC 기준 — wrangler.toml의 [triggers] crons 값과 반드시 같아야 합니다.
const PRIMARY_CRON = "0 3 * * 1-5"; // 평일 한국시간 12:00
const BACKUP_CRON = "0 4 * * 1-5";  // 평일 한국시간 13:00 (백업)

const KST_OFFSET_MS = 9 * 60 * 60 * 1000;

function kstDate(d = new Date()) {
  return new Date(d.getTime() + KST_OFFSET_MS); // getUTC* 메서드로 읽으면 한국시간 값
}

function kstDateString(d = new Date()) {
  return kstDate(d).toISOString().slice(0, 10); // YYYY-MM-DD (한국시간)
}

function githubHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Scanner-Worker",
  };
}

/** 오늘(한국시간) 성공한 Scanner 실행이 있는지 확인. 확인에 실패하면 null 반환. */
async function hasSuccessfulRunToday(token) {
  const today = kstDateString();
  try {
    const url =
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/runs` +
      `?branch=${BRANCH}&status=success&per_page=20`;
    const res = await fetch(url, { headers: githubHeaders(token) });
    if (!res.ok) {
      console.log(`실행 이력 확인 실패 (HTTP ${res.status}) — 안전하게 실행 진행`);
      return null;
    }
    const data = await res.json();
    const runs = data.workflow_runs || [];
    const found = runs.find(
      (r) => r.conclusion === "success" && kstDateString(new Date(r.created_at)) === today
    );
    if (found) {
      console.log(`오늘(${today}) 이미 성공한 실행 있음: #${found.run_number} ${found.html_url}`);
      return true;
    }
    console.log(`오늘(${today}) 성공한 실행 없음`);
    return false;
  } catch (e) {
    console.log(`실행 이력 확인 중 오류 — 안전하게 실행 진행: ${e}`);
    return null;
  }
}

/** Scanner 워크플로 실행 (workflow_dispatch). 성공 시 GitHub는 204를 돌려줍니다. */
async function dispatchWorkflow(token) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: { ...githubHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: BRANCH }), // inputs 생략 → codes 비움 = 전체 스캔
  });
  if (res.status === 204) {
    console.log("GitHub Actions 실행 요청 성공 (204)");
    return;
  }
  const detail = await res.text().catch(() => "");
  // 실패를 Cloudflare 로그/대시보드에 오류로 남기기 위해 throw
  throw new Error(`GitHub Actions 실행 요청 실패: HTTP ${res.status} ${detail}`);
}

export default {
  async scheduled(event, env, ctx) {
    const isBackup = event.cron === BACKUP_CRON;
    const label = isBackup ? "2차(백업)" : "1차";
    const now = kstDate();
    console.log(`[${label}] 트리거 ${event.cron} | 한국시간 ${now.toISOString().slice(0, 16).replace("T", " ")}`);

    // 일요일(한국시간)은 둘 다 건너뜀
    if (now.getUTCDay() === 0) {
      console.log("일요일 — 건너뜀");
      return;
    }

    const token = env.GH_TOKEN;
    if (!token) {
      throw new Error("GH_TOKEN Secret이 없습니다. `npx wrangler secret put GH_TOKEN`으로 등록하세요.");
    }

    if (isBackup) {
      const alreadyRan = await hasSuccessfulRunToday(token);
      if (alreadyRan === true) {
        console.log("백업 실행 건너뜀");
        return;
      }
      // false(성공 없음) 또는 null(확인 실패) → 실행
    }

    await dispatchWorkflow(token);
  },
};
