const root = document.documentElement;
const body = document.body;
const mobileToggle = document.querySelector('[data-mobile-menu]');
const sidebar = document.querySelector('.sidebar');
const themeToggle = document.querySelector('[data-theme-toggle]');

function setTheme(theme) {
  body.classList.toggle('dark', theme === 'dark');
  localStorage.setItem('theme', theme);
}

if (mobileToggle && sidebar) {
  mobileToggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
  });
}

const storedTheme = localStorage.getItem('theme');
if (storedTheme) {
  setTheme(storedTheme);
} else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
  setTheme('dark');
}

if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const nextTheme = body.classList.contains('dark') ? 'light' : 'dark';
    setTheme(nextTheme);
  });
}

document.querySelectorAll('.comparison-fill').forEach((bar) => {
  const updateFocus = () => {
    const focus = document.getElementById('comparison-date-focus');
    if (!focus) return;
    focus.textContent = `${bar.dataset.label}: ${bar.dataset.date}`;
  };

  bar.addEventListener('click', updateFocus);
  bar.addEventListener('mouseenter', updateFocus);
  bar.setAttribute('tabindex', '0');
  bar.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      updateFocus();
    }
  });
});

document.querySelectorAll('.legend-pill').forEach((pill) => {
  pill.addEventListener('click', () => {
    const focus = document.getElementById('comparison-date-focus');
    if (!focus) return;
    focus.textContent = pill.dataset.dateLabel;
  });
});

window.addEventListener('click', (event) => {
  if (sidebar && mobileToggle && window.innerWidth < 1024 && sidebar.classList.contains('open') && !sidebar.contains(event.target) && !mobileToggle.contains(event.target)) {
    sidebar.classList.remove('open');
  }
});

document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', () => {
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    });
  });
});

document.querySelectorAll('[data-auto-submit]').forEach((control) => {
  control.addEventListener('change', () => control.form?.requestSubmit());
});

document.querySelectorAll('[data-confirm]').forEach((control) => {
  control.addEventListener('click', (event) => {
    if (!window.confirm(control.dataset.confirm)) event.preventDefault();
  });
});
