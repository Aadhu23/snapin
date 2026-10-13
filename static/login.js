const loginForm = document.getElementById('login-form');
const errorEl = document.getElementById('login-error');
const submitBtn = loginForm?.querySelector('button[type="submit"]');

function showError(message) {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.add('visible');
}

function clearError() {
    if (!errorEl) return;
    errorEl.textContent = '';
    errorEl.classList.remove('visible');
}

// Already signed in? Skip the form entirely.
(async function checkExistingSession() {
    try {
        const res = await fetch('/api/me', { credentials: 'same-origin' });
        if (res.ok) window.location.replace('dashboard.html');
    } catch {
        /* Offline or server down: just show the login form. */
    }
})();

if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        clearError();

        const username = loginForm.querySelector('input[type="text"]').value.trim();
        const password = loginForm.querySelector('input[type="password"]').value;

        if (!username || !password) {
            showError('Enter both a username and a password.');
            return;
        }

        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Signing in...';
        }

        try {
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ username, password }),
            });

            if (res.ok) {
                window.location.href = 'dashboard.html';
                return;
            }

            let detail = 'Login failed.';
            try {
                const data = await res.json();
                if (data && data.detail) detail = data.detail;
            } catch {
                /* Non-JSON error body. */
            }
            showError(detail);
        } catch (err) {
            showError('Could not reach the server. Is it running?');
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Enter Workspace';
            }
        }
    });
} else {
    console.error('Login form not found!');
}