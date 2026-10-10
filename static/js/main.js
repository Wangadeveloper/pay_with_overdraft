/**
 * PayFlow Overdraft — Main JavaScript
 * Toast notifications, shared utilities
 */

// ─── Toast Notification System ─────────────────────────────────────
function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span style="font-size:1.1rem">${icons[type] || 'ℹ️'}</span>
    <span style="flex:1">${message}</span>
    <button onclick="this.parentElement.remove()" style="background:none;border:none;color:inherit;cursor:pointer;opacity:0.6;font-size:1rem;padding:0 0 0 8px">✕</button>
  `;

  container.appendChild(toast);

  // Auto-remove
  setTimeout(() => {
    toast.style.animation = 'toastOut 0.3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ─── Navbar scroll effect ───────────────────────────────────────────
window.addEventListener('scroll', () => {
  const nav = document.getElementById('navbar');
  if (!nav) return;
  if (window.scrollY > 10) {
    nav.style.boxShadow = '0 4px 30px rgba(0,0,0,0.4)';
  } else {
    nav.style.boxShadow = 'none';
  }
}, { passive: true });

// ─── Animate numbers on page load ──────────────────────────────────
function animateValue(el, start, end, duration = 800) {
  if (!el) return;
  const startTime = performance.now();
  const isFloat = end !== Math.floor(end);

  function update(currentTime) {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);
    // Ease out cubic
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = start + (end - start) * eased;
    el.textContent = '$' + (isFloat ? current.toFixed(2) : Math.floor(current));
    if (progress < 1) requestAnimationFrame(update);
  }
  requestAnimationFrame(update);
}

// Animate all .count-up elements & initialize settings display
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.count-up').forEach(el => {
    const target = parseFloat(el.dataset.target || el.textContent.replace('$', ''));
    animateValue(el, 0, target);
  });
  loadUserSettingsNav();
});

// ─── Sandbox Account Settings Modal ─────────────────────────────────
async function loadUserSettingsNav() {
  const el = document.getElementById('nav-user-email');
  if (!el) return;
  try {
    const res = await fetch('/api/user/settings');
    const data = await res.json();
    if (data.user && data.user.paypal_email) {
      el.textContent = data.user.paypal_email;
      el.title = `Connected PayPal Sandbox Email: ${data.user.paypal_email}`;
    }
  } catch (e) {
    // legacy fallback
  }
}

async function openSandboxSettingsModal() {
  const modal = document.getElementById('sandbox-settings-modal');
  if (!modal) return;
  try {
    const res = await fetch('/api/user/settings');
    const data = await res.json();
    if (data.user) {
      const emailEl = document.getElementById('settings-buyer-email');
      const nameEl = document.getElementById('settings-buyer-name');
      if (emailEl) emailEl.value = data.user.paypal_email || '';
      if (nameEl) nameEl.value = data.user.name || '';
    }
  } catch (e) {}
  modal.classList.add('active');
}

function closeSandboxSettingsModal() {
  const modal = document.getElementById('sandbox-settings-modal');
  if (modal) modal.classList.remove('active');
}

async function saveSandboxSettings() {
  const email = document.getElementById('settings-buyer-email').value.trim();
  const name = document.getElementById('settings-buyer-name').value.trim();

  if (!email) {
    showToast('Please enter a valid Personal Sandbox Email', 'error');
    return;
  }

  showToast('Saving sandbox account settings...', 'info');

  try {
    const res = await fetch('/api/user/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        paypal_email: email,
        name: name || 'Sandbox Buyer'
      })
    });
    const data = await res.json();

    if (data.success) {
      showToast(data.message, 'success');
      closeSandboxSettingsModal();
      loadUserSettingsNav();
      setTimeout(() => location.reload(), 1000);
    } else {
      showToast(data.error || 'Failed to save settings', 'error');
    }
  } catch (e) {
    showToast('Network error saving settings: ' + e.message, 'error');
  }
}
