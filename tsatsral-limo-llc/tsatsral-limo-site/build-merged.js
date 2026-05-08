const fs = require('fs');
const path = require('path');

const dir = __dirname;

// Read all page files
const home = fs.readFileSync(path.join(dir, 'index/index.html'), 'utf8');
const services = fs.readFileSync(path.join(dir, 'services/index.html'), 'utf8');
const blog = fs.readFileSync(path.join(dir, 'blog/index.html'), 'utf8');
const about = fs.readFileSync(path.join(dir, 'about/index.html'), 'utf8');
const contact = fs.readFileSync(path.join(dir, 'contact/index.html'), 'utf8');

// Extract content between <body> and </body>
function extractBody(html) {
  const m = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  return m ? m[1] : '';
}

// Extract content between <style> and </style>
function extractStyles(html) {
  const styles = [];
  const re = /<style[^>]*>([\s\S]*?)<\/style>/gi;
  let m;
  while ((m = re.exec(html)) !== null) styles.push(m[1]);
  return styles.join('\n');
}

// Extract content between <script> and </script> (only inline, skip the localStorage shim)
function extractScripts(html) {
  const scripts = [];
  const re = /<script[^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    if (!m[1].includes('makeStore') && m[1].trim().length > 10) {
      scripts.push(m[1]);
    }
  }
  return scripts;
}

// Extract the main content - everything between nav closing and footer opening
// But we need announce bar, nav, main content, and footer separately
function extractBetween(body, startAfter, endBefore) {
  const si = body.indexOf(startAfter);
  const ei = body.indexOf(endBefore);
  if (si === -1 || ei === -1) return body;
  return body.substring(si + startAfter.length, ei).trim();
}

function extractMainContent(body) {
  // Remove announce bar
  let content = body.replace(/<div class="announce">[\s\S]*?<\/div>\s*/i, '');
  // Remove nav
  content = content.replace(/<nav class="topnav">[\s\S]*?<\/nav>\s*/i, '');
  // Remove footer
  content = content.replace(/<footer>[\s\S]*?<\/footer>\s*/i, '');
  // Remove any standalone script tags
  content = content.replace(/<script>[\s\S]*?<\/script>\s*/gi, '');
  return content.trim();
}

// Get the shared nav from home page, but make links use onclick routing
const homeBody = extractBody(home);

// Extract announce bar from home
const announceMatch = homeBody.match(/<div class="announce">[\s\S]*?<\/div>/i);
const announceBar = announceMatch ? announceMatch[0] : '';

// Extract nav from home
const navMatch = homeBody.match(/<nav class="topnav">[\s\S]*?<\/nav>/i);
let sharedNav = navMatch ? navMatch[0] : '';

// Extract footer from home
const footerMatch = homeBody.match(/<footer>[\s\S]*?<\/footer>/i);
let sharedFooter = footerMatch ? footerMatch[0] : '';

// Fix nav links to use hash routing
sharedNav = sharedNav
  .replace(/href="\/"/g, 'href="#home"')
  .replace(/href="index\.html"/g, 'href="#home"')
  .replace(/href="services\.html"/g, 'href="#services"')
  .replace(/href="blog\.html"/g, 'href="#blog"')
  .replace(/href="about\.html"/g, 'href="#about"')
  .replace(/href="contact\.html"/g, 'href="#contact"')
  .replace(/href="#fleet"/g, 'href="#home" data-scroll="fleet"')
  .replace(/href="#how"/g, 'href="#home" data-scroll="how"')
  .replace(/class="has-caret"/g, '')
  .replace(/<a ([^>]*?)href="#home"([^>]*?)>Solutions<\/a>/i, '<a $1href="#home"$2 data-nav="home">Solutions</a>')
  .replace(/<a ([^>]*?)href="#services"([^>]*?)>Services<\/a>/i, '<a $1href="#services"$2 data-nav="services">Services</a>')
  .replace(/<a ([^>]*?)href="#blog"([^>]*?)>Resources<\/a>/i, '<a $1href="#blog"$2 data-nav="blog">Resources</a>')
  .replace(/<a ([^>]*?)href="#about"([^>]*?)>About<\/a>/i, '<a $1href="#about"$2 data-nav="about">About</a>');

// Fix footer links too
sharedFooter = sharedFooter
  .replace(/href="\/"/g, 'href="#home"')
  .replace(/href="index\.html(#[^"]*)?"/g, (m, hash) => `href="#home"${hash ? ` data-scroll="${hash.slice(1)}"` : ''}`)
  .replace(/href="services\.html(#[^"]*)?"/g, (m, hash) => `href="#services"${hash ? ` data-scroll="${hash.slice(1)}"` : ''}`)
  .replace(/href="blog\.html"/g, 'href="#blog"')
  .replace(/href="about\.html"/g, 'href="#about"')
  .replace(/href="contact\.html"/g, 'href="#contact"');

// Fix announce bar links
let fixedAnnounce = announceBar
  .replace(/href="contact\.html"/g, 'href="#contact"')
  .replace(/href="#demo"/g, 'href="#contact"');

// Get main content from each page
const homeContent = extractMainContent(extractBody(home));
const servicesContent = extractMainContent(extractBody(services));
const blogContent = extractMainContent(extractBody(blog));
const aboutContent = extractMainContent(extractBody(about));
const contactContent = extractMainContent(extractBody(contact));

// Fix internal links within page content
function fixInternalLinks(html) {
  return html
    .replace(/href="index\.html(#[^"]*)?"/g, (m, hash) => `href="#home"${hash ? ` data-scroll="${hash.slice(1)}"` : ''}`)
    .replace(/href="services\.html(#[^"]*)?"/g, (m, hash) => `href="#services"${hash ? ` data-scroll="${hash.slice(1)}"` : ''}`)
    .replace(/href="blog\.html"/g, 'href="#blog"')
    .replace(/href="about\.html"/g, 'href="#about"')
    .replace(/href="contact\.html"/g, 'href="#contact"');
}

// Collect all CSS
const allStyles = [
  extractStyles(home),
  '\n/* === SERVICES PAGE STYLES === */\n' + extractStyles(services),
  '\n/* === BLOG PAGE STYLES === */\n' + extractStyles(blog),
  '\n/* === ABOUT PAGE STYLES === */\n' + extractStyles(about),
  '\n/* === CONTACT PAGE STYLES === */\n' + extractStyles(contact),
].join('\n');

// Collect page-specific scripts
const homeScripts = extractScripts(home);
const servicesScripts = extractScripts(services);

// De-duplicate CSS: we'll keep all of it since browser handles duplicates fine
// But add the SPA-specific styles
const spaStyles = `
/* === SPA ROUTING === */
.page-section { display: none; }
.page-section.is-active { display: block; }
.nav a[data-nav].is-active { color: var(--fg); border-bottom: 1px solid var(--accent); }
`;

// Build the final HTML
const merged = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tsatsral Limo — The operating system for chauffeured ground transport</title>
<meta name="description" content="Tsatsral is the operating system for chauffeured ground transport — reservations, dispatch, drivers, billing. One platform, one schedule, one source of truth.">
<style>
${allStyles}
${spaStyles}
</style>
</head>
<body>

${fixedAnnounce}

${sharedNav}

<!-- ====== HOME PAGE ====== -->
<div class="page-section is-active" data-page="home">
${fixInternalLinks(homeContent)}
</div>

<!-- ====== SERVICES PAGE ====== -->
<div class="page-section" data-page="services">
${fixInternalLinks(servicesContent)}
</div>

<!-- ====== BLOG PAGE ====== -->
<div class="page-section" data-page="blog">
${fixInternalLinks(blogContent)}
</div>

<!-- ====== ABOUT PAGE ====== -->
<div class="page-section" data-page="about">
${fixInternalLinks(aboutContent)}
</div>

<!-- ====== CONTACT PAGE ====== -->
<div class="page-section" data-page="contact">
${fixInternalLinks(contactContent)}
</div>

${sharedFooter}

<script>
(function(){
  // ── Pill / spotlight switcher (home page) ──
  ${homeScripts.join('\n')}

  // ── TOC scroll-spy (services page) ──
  ${servicesScripts.join('\n')}

  // ── SPA Router ──
  const sections = document.querySelectorAll('.page-section');
  const navLinks = document.querySelectorAll('.nav a[data-nav]');

  function navigate(page, scrollTarget) {
    // Show the right section
    sections.forEach(s => s.classList.toggle('is-active', s.dataset.page === page));
    // Update nav active state
    navLinks.forEach(a => a.classList.toggle('is-active', a.dataset.nav === page));
    // Scroll to top or to a specific element
    if (scrollTarget) {
      const el = document.getElementById(scrollTarget);
      if (el) { setTimeout(() => el.scrollIntoView({behavior:'smooth'}), 50); return; }
    }
    window.scrollTo(0, 0);
  }

  // Handle all link clicks with hash hrefs
  document.addEventListener('click', function(e) {
    const link = e.target.closest('a[href^="#"]');
    if (!link) return;
    const href = link.getAttribute('href');
    const scrollTarget = link.dataset.scroll;
    
    // Map hash to page
    const page = href.replace('#', '');
    const validPages = ['home','services','blog','about','contact'];
    
    if (validPages.includes(page)) {
      e.preventDefault();
      history.pushState(null, '', '#' + page);
      navigate(page, scrollTarget);
    }
  });

  // Handle browser back/forward
  window.addEventListener('popstate', function() {
    const hash = location.hash.replace('#', '') || 'home';
    navigate(hash);
  });

  // Handle initial load with hash
  const initialHash = location.hash.replace('#', '') || 'home';
  if (initialHash !== 'home') navigate(initialHash);
})();
</script>

</body>
</html>`;

fs.writeFileSync(path.join(dir, 'index.html'), merged, 'utf8');
console.log('✅ Merged index.html written (' + merged.length + ' bytes, ' + merged.split('\\n').length + ' lines)');
