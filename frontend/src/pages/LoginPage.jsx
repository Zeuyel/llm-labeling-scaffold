import React, { useState } from "react";

export default function LoginPage({ onLogin, error = "" }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");

  async function submit(event) {
    event.preventDefault();
    if (!username.trim() || !password) {
      setFormError("请输入用户名和密码");
      return;
    }
    setBusy(true);
    setFormError("");
    try {
      await onLogin(username.trim(), password);
    } catch (requestError) {
      setFormError(String(requestError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-panel" aria-labelledby="login-title">
        <div className="auth-brand-mark">标</div>
        <h1 id="login-title">标注控制台</h1>
        <p className="auth-subtitle">登录后管理任务、数据和标注流程</p>
        {(formError || error) && <div className="auth-error" role="alert">{formError || error}</div>}
        <form className="auth-form" onSubmit={submit}>
          <label className="field">
            <span>用户名</span>
            <input
              value={username}
              autoComplete="username"
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              value={password}
              type="password"
              autoComplete="current-password"
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
            {busy ? "登录中..." : "登录"}
          </button>
        </form>
      </section>
    </main>
  );
}
