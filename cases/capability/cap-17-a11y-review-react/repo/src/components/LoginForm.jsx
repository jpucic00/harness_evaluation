import { useId } from "react";

export function LoginForm({ onSubmit, error }) {
  const id = useId();
  return (
    <form onSubmit={onSubmit} aria-describedby={error ? `${id}-error` : undefined}>
      <h2>Sign in</h2>
      <label htmlFor={`${id}-email`}>Email</label>
      <input id={`${id}-email`} type="email" autoComplete="email" required />
      <label htmlFor={`${id}-password`}>Password</label>
      <input id={`${id}-password`} type="password" autoComplete="current-password" required />
      {error && (
        <p id={`${id}-error`} role="alert">
          {error}
        </p>
      )}
      <button type="submit">Sign in</button>
    </form>
  );
}
