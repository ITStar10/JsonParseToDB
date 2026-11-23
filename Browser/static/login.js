/* Sign-in and sign-up page. */
(() => {
  "use strict";
  const { $, api, bindTheme } = window.UI;
  window.UI.noAuthRedirect = true;

  function show(which) {
    const signin = which === "signin";
    $("signinForm").hidden = !signin;
    $("signupForm").hidden = signin;
    $("signupDone").hidden = true;
    $("tabSignin").classList.toggle("on", signin);
    $("tabSignup").classList.toggle("on", !signin);
    $("tabSignin").setAttribute("aria-selected", signin);
    $("tabSignup").setAttribute("aria-selected", !signin);
    $(signin ? "siEmail" : "suName").focus();
    history.replaceState(null, "", signin ? "/login" : "/login#signup");
  }

  async function submitting(btn, label, fn) {
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = label;
    try { await fn(); } finally { btn.disabled = false; btn.textContent = old; }
  }

  $("signinForm").onsubmit = (e) => {
    e.preventDefault();
    const email = $("siEmail").value.trim(), password = $("siPassword").value;
    $("siError").textContent = "";
    if (!email || !password) { $("siError").textContent = "Enter your e-mail and password."; return; }
    submitting($("siSubmit"), "Signing in…", async () => {
      try {
        await api("/api/auth/login", { method: "POST", body: { email, password } });
        location.href = "/";
      } catch (err) {
        $("siError").textContent = err.message;
        $("siError").classList.toggle("info", err.code === "pending");
        $("siPassword").select();
      }
    });
  };

  $("signupForm").onsubmit = (e) => {
    e.preventDefault();
    const fullName = $("suName").value.trim(), email = $("suEmail").value.trim();
    const password = $("suPassword").value, confirm = $("suConfirm").value;
    const err = $("suError");
    err.textContent = "";
    if (!fullName) { err.textContent = "Enter your full name."; return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { err.textContent = "Enter a valid e-mail address."; return; }
    if (password.length < 8) { err.textContent = "Use at least 8 characters for your password."; return; }
    if (password !== confirm) { err.textContent = "The passwords don’t match."; return; }
    submitting($("suSubmit"), "Sending…", async () => {
      try {
        const res = await api("/api/auth/signup", { method: "POST", body: { fullName, email, password } });
        $("signupForm").reset();
        $("signupForm").hidden = true;
        $("signupDone").hidden = false;
        $("doneText").textContent = res.message;
        $("siEmail").value = email;
      } catch (ex) { err.textContent = ex.message; }
    });
  };

  $("tabSignin").onclick = $("toSignin").onclick = $("doneBack").onclick = () => show("signin");
  $("tabSignup").onclick = $("toSignup").onclick = () => show("signup");

  bindTheme($("themeBtn"));
  show(location.hash === "#signup" ? "signup" : "signin");
  api("/api/auth/me").then((r) => {
    if (r.user) location.href = "/";
    $("noAdminNote").hidden = r.hasAdmin;
  }).catch(() => {});
})();
