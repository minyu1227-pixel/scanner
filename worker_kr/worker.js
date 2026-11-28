/**
 * Cloudflare Worker (국내주식) — GitHub Actions "Scanner KR"(scan_kr.yml)를 평일 장 마감 후 실행
 * 미국용 worker.js / wrangler.toml 과 별개의 워커입니다 (이름: scanner-kr-worker).
 *
 *  1차 (PRIMARY_CRON) : 평일 16:30 KST — 무조건 workflow_dispatch 호출
 *  2차 (BACKUP_CRON)  : 평일 17:30 KST — 오늘(한국시간) 성공한 Scanner KR 실행이 있으면 건너뜀
 *  주말(한국시간)      : 건너뜀 (KRX 휴장일은 scanner_kr.py가 최근 봉 날짜로 판단해 시그널을 보내지 않음)
 *
 *  GitHub 토큰은 Cloudflare Secret(GH_TOKEN)에서 읽습니다.
 *    등록: cd worker_kr && npx wrangler secret put GH_TOKEN
 */

const REPO = "minyu1227-pixel/scanner";
const WORKFLOW_FILE = "scan_kr.yml";
const BRANCH = "main";

// cron은 UTC 기준 — wrangler.toml의 [triggers] crons 값과 같아야 합니다.
const PRIMARY_CRON = "30 7 * * 1-5"; // 평일 한국시간 16:30
const BACKUP_CRON = "30 8 * * 1-5";  // 평일 한국시간 17:30 (백업)

const KST_OFFSET_MS = 9 * 60 * 60 * 1000;

function kstDate(d = new Date()) {
  return new Date(d.getTime() + KST_OFFSET_MS);
}

function kstDateString(d = new Date()) {
  return kstDate(d).toISOString().slice(0, 10);
}

function githubHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Scanner-KR-Worker",
  };
}

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
    const found = (data.workflow_runs || []).find(
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

async function dispatchWorkflow(token) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: { ...githubHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: BRANCH }),
  });
  if (res.status === 204) {
    console.log("GitHub Actions(Scanner KR) 실행 요청 성공 (204)");
    return;
  }
  const detail = await res.text().catch(() => "");
  throw new Error(`GitHub Actions(Scanner KR) 실행 요청 실패: HTTP ${res.status} ${detail}`);
}

export default {
  async scheduled(event, env, ctx) {
    const isBackup = event.cron === BACKUP_CRON;
    const label = isBackup ? "2차(백업)" : "1차";
    const now = kstDate();
    console.log(`[KR ${label}] 트리거 ${event.cron} | 한국시간 ${now.toISOString().slice(0, 16).replace("T", " ")}`);

    const day = now.getUTCDay();
    if (day === 0 || day === 6) {
      console.log("주말 — 건너뜀");
      return;
    }

    const token = env.GH_TOKEN;
    if (!token) {
      throw new Error("GH_TOKEN Secret이 없습니다. `cd worker_kr && npx wrangler secret put GH_TOKEN`으로 등록하세요.");
    }

    if (isBackup) {
      const alreadyRan = await hasSuccessfulRunToday(token);
      if (alreadyRan === true) {
        console.log("백업 실행 건너뜀");
        return;
      }
    }

    await dispatchWorkflow(token);
  },
};
