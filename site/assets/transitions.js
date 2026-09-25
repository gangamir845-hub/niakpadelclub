(function(){
  const key='niak-theme';
  // Cookie-based so it also works inside sandboxed preview iframes.
  // If cookies are blocked, the theme simply follows the OS setting.
  const store={
    get(){
      try{
        const m=document.cookie.match(new RegExp('(?:^|; )'+key+'=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : null;
      }catch(e){ return null; }
    },
    set(v){
      try{ document.cookie=key+'='+encodeURIComponent(v)+'; path=/; max-age=31536000; samesite=lax'; }catch(e){}
    }
  };
  const saved=store.get();
  const systemDark=window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = saved || (systemDark ? 'dark' : 'light');

  function icon(){
    return document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';
  }

  function applyTheme(next){
    document.documentElement.dataset.theme = next;
    store.set(next);
  }

  // انیمیشن باز شدن دایره‌ای از نقطه‌ی کلیک، با کمک View Transition API.
  // کوتاه و یک‌باره‌ست (حدود نیم‌ثانیه) که خسته‌کننده نشه، و اگر کاربر
  // ترجیح می‌ده حرکت کمتری ببینه (prefers-reduced-motion) کاملاً غیرفعال می‌شه.
  // مرورگرهایی که View Transitions رو ندارن (مثلاً فایرفاکس) یه محو نرم
  // جایگزین می‌گیرن، نه یه پرش خشک بین دو حالت.
  function toggleThemeAnimated(next, x, y){
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(reduceMotion){ applyTheme(next); return; }

    if(typeof document.startViewTransition === 'function'){
      const cx = x != null ? x : window.innerWidth / 2;
      const cy = y != null ? y : window.innerHeight / 2;
      const endRadius = Math.hypot(
        Math.max(cx, window.innerWidth - cx),
        Math.max(cy, window.innerHeight - cy)
      );
      const transition = document.startViewTransition(() => applyTheme(next));
      transition.ready.then(() => {
        document.documentElement.animate(
          { clipPath: [`circle(0px at ${cx}px ${cy}px)`, `circle(${endRadius}px at ${cx}px ${cy}px)`] },
          { duration: 480, easing: 'ease-in-out', pseudoElement: '::view-transition-new(root)' }
        );
      }).catch(() => {});
    } else {
      document.documentElement.classList.add('theme-fade-fallback');
      applyTheme(next);
      setTimeout(() => document.documentElement.classList.remove('theme-fade-fallback'), 400);
    }
  }

  function addToggle(){
    if(document.querySelector('.theme-toggle')) return;
    const header=document.querySelector('.site-header');
    const wrap=header && header.querySelector('.wrap');
    const btn=document.createElement('button');
    btn.className='theme-toggle';
    btn.type='button';
    btn.setAttribute('aria-label','تغییر حالت شب و روز');
    btn.title='تغییر حالت شب و روز';
    btn.textContent=icon();
    btn.addEventListener('click',(e)=>{
      const next=document.documentElement.dataset.theme==='dark'?'light':'dark';
      toggleThemeAnimated(next, e.clientX, e.clientY);
      btn.textContent=icon();
    });
    if(wrap){
      wrap.appendChild(btn);
    }else{
      // pages without a site header (homepage) get a floating toggle
      btn.classList.add('theme-toggle-float');
      document.body.appendChild(btn);
    }
  }
  document.addEventListener('DOMContentLoaded',addToggle);
})();

(function(){
  // ---- click ripple effect ----
  function addRipple(e){
    const el = e.currentTarget;
    if(el.disabled) return;
    const rect = el.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 1.3;
    const ripple = document.createElement('span');
    ripple.className = 'ripple';
    ripple.style.width = ripple.style.height = size + 'px';
    ripple.style.left = (e.clientX - rect.left - size / 2) + 'px';
    ripple.style.top = (e.clientY - rect.top - size / 2) + 'px';
    el.appendChild(ripple);
    ripple.addEventListener('animationend', () => ripple.remove());
  }

  function initRipples(root){
    (root || document).querySelectorAll('.btn, .nav-card, .court-card, .dur-btn, .slot').forEach(el => {
      if(el.dataset.rippleBound) return;
      el.classList.add('ripple-btn');
      el.addEventListener('click', addRipple);
      el.dataset.rippleBound = '1';
    });
  }
  window.initRipples = initRipples;
  document.addEventListener('DOMContentLoaded', () => initRipples());

  // ---- fade transition when moving between pages/folders ----
  document.addEventListener('click', function(e){
    const link = e.target.closest('a[href]');
    if(!link) return;
    const href = link.getAttribute('href');
    if(!href || href.startsWith('#') || href.startsWith('http') || href.startsWith('mailto:') || link.target === '_blank') return;
    e.preventDefault();
    document.body.classList.add('page-leaving');
    setTimeout(() => { window.location.href = href; }, 210);
  });
})();
