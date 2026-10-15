const logoutBtn = document.getElementById('logout-btn');
const errorBanner = document.getElementById('stats-error');
const errorText = document.getElementById('stats-error-text');
const rangeLabel = document.getElementById('range-label');
const chartEmpty = document.getElementById('chart-empty');
const recentEmpty = document.getElementById('recent-empty');
const episodesEl = document.getElementById('episodes');

let rangeDays = 7;

// --- Formatting ---------------------------------------------------------
// Kept deliberately small. A charting library would be more code than the
// nine bars it draws.

function formatDuration(seconds) {
    if (!seconds || seconds < 60) return seconds ? `${seconds}s` : '0s';

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.round((seconds % 3600) / 60);
    if (hours === 0) return `${minutes}m`;
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

function formatClock(iso) {
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return '-';
    return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatDayLabel(iso) {
    const day = new Date(`${iso}T00:00:00`);
    return day.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

// --- Renderers ----------------------------------------------------------

function renderSummary(summary) {
    document.getElementById('m-focus').textContent = formatDuration(summary.focus_seconds);
    document.getElementById('m-focus-note').textContent =
        summary.focus_ratio == null
            ? 'no sessions yet'
            : `${Math.round(summary.focus_ratio * 100)}% uninterrupted`;

    document.getElementById('m-episodes').textContent = String(summary.episodes);
    document.getElementById('m-episodes-note').textContent =
        summary.detections_per_session == null
            ? 'no sessions yet'
            : `${summary.detections_per_session} per session`;

    document.getElementById('m-sessions').textContent = String(summary.sessions);
    document.getElementById('m-sessions-note').textContent =
        summary.sessions === 1 ? 'study block' : 'study blocks';

    document.getElementById('m-distracted').textContent = formatDuration(summary.distracted_seconds);
    document.getElementById('m-distracted-note').textContent =
        summary.episodes === 0 ? 'clean run' : `${summary.episodes} pickup${summary.episodes === 1 ? '' : 's'}`;
}

/**
 * Stacked bars: focus time in white, phone time stacked on top in red. The
 * red portion is what the user is trying to shrink, so it reads as an
 * overlay on the day rather than a separate series.
 */
function renderDaily(daily) {
    const chart = document.getElementById('chart-daily');
    chart.textContent = '';

    const hasData = daily.some((d) => d.focus_seconds > 0);
    chartEmpty.hidden = hasData;
    chart.style.display = hasData ? '' : 'none';

    const peak = Math.max(...daily.map((d) => d.focus_seconds), 1);

    for (const day of daily) {
        const col = document.createElement('div');
        col.className = 'bar-col';
        col.title = `${day.date}: ${formatDuration(day.focus_seconds)} focus, ${day.episodes} pickup(s)`;

        const stack = document.createElement('div');
        stack.className = 'bar-stack';

        // Phone time as a share of that day's focus time, capped at 100 so a
        // bad clock cannot overflow the column.
        const phoneShare = Math.min(
            day.focus_seconds > 0 ? day.distracted_seconds / day.focus_seconds : 0,
            1
        );

        const phone = document.createElement('div');
        phone.className = 'bar bar-phone';
        phone.style.height = `${(day.focus_seconds / peak) * 100 * phoneShare}%`;

        const focus = document.createElement('div');
        focus.className = 'bar bar-focus';
        focus.style.height = `${(day.focus_seconds / peak) * 100 * (1 - phoneShare)}%`;

        stack.append(phone, focus);

        const label = document.createElement('span');
        label.className = 'bar-label';
        label.textContent = formatDayLabel(day.date);

        col.append(stack, label);
        chart.appendChild(col);
    }
}

function renderHourly(hourly) {
    const chart = document.getElementById('chart-hourly');
    chart.textContent = '';

    const peak = Math.max(...hourly.map((h) => h.episodes), 1);

    hourly.forEach((bucket) => {
        const col = document.createElement('div');
        col.className = 'bar-col';
        // Only label every third hour, otherwise the labels collide.
        col.title = `${String(bucket.hour).padStart(2, '0')}:00 — ${bucket.episodes} pickup(s)`;

        const stack = document.createElement('div');
        stack.className = 'bar-stack';

        const bar = document.createElement('div');
        bar.className = 'bar bar-phone';
        bar.style.height = `${(bucket.episodes / peak) * 100}%`;
        stack.appendChild(bar);

        const label = document.createElement('span');
        label.className = 'bar-label';
        label.textContent = bucket.hour % 3 === 0 ? String(bucket.hour) : '';

        col.append(stack, label);
        chart.appendChild(col);
    });
}

function renderRecent(recent) {
    episodesEl.textContent = '';
    recentEmpty.hidden = recent.length > 0;

    for (const item of recent) {
        const li = document.createElement('li');
        li.className = 'episode';

        const when = document.createElement('span');
        when.className = 'episode-when';
        when.textContent = formatClock(item.started_at);

        const sep = document.createElement('span');
        sep.className = 'episode-sep';
        sep.textContent = '·';

        const meta = document.createElement('span');
        meta.className = 'episode-meta';
        meta.textContent = `conf ${Number(item.peak_confidence).toFixed(2)} · ${item.frames}f`;

        const dur = document.createElement('span');
        dur.className = 'episode-dur';
        dur.textContent = formatDuration(item.seconds);

        li.append(when, sep, meta, dur);
        episodesEl.appendChild(li);
    }
}

// --- Loading ------------------------------------------------------------

function showError(message) {
    errorText.textContent = message;
    errorBanner.classList.add('visible');
}

async function load() {
    errorBanner.classList.remove('visible');
    rangeLabel.textContent = `Last ${rangeDays} days`;

    try {
        const res = await fetch(`/api/stats?days=${rangeDays}`, {
            credentials: 'same-origin',
        });

        if (res.status === 401) {
            location.replace('index.html');
            return;
        }
        if (!res.ok) throw new Error(`Request failed (${res.status})`);

        const data = await res.json();
        if (data.status !== 'ok') throw new Error(data.error || 'Could not load statistics.');

        renderSummary(data.summary);
        renderDaily(data.daily);
        renderHourly(data.hourly);
        renderRecent(data.recent);
    } catch (err) {
        showError('Could not load statistics. Is the server still running?');
    }
}

// --- Controls -----------------------------------------------------------

document.querySelectorAll('.segmented-btn').forEach((button) => {
    if (Number(button.dataset.days) === rangeDays) {
        button.setAttribute('aria-pressed', 'true');
    }

    button.addEventListener('click', () => {
        document.querySelectorAll('.segmented-btn').forEach((b) => {
            b.setAttribute('aria-pressed', String(b === button));
        });
        rangeDays = Number(button.dataset.days);
        load();
    });
});

logoutBtn?.addEventListener('click', async () => {
    try {
        await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
        /* Local sign-out is enough even if the request fails. */
    }
    location.replace('index.html');
});

load();