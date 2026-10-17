import { API_BASE_URL } from "./config.js";

function prefillEmail() {
  const emailInput = document.getElementById("email");
  const storedEmail = sessionStorage.getItem("prefill_email");
  if (emailInput && storedEmail) {
    emailInput.value = storedEmail;
    sessionStorage.removeItem("prefill_email");
  }
}

function setStatus(element, message, tone = "") {
  element.textContent = message;
  element.className = `auth-status ${tone}`.trim();
}

async function submitAuth(url, body) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 10000);

  let response;
  try {
    response = await fetch(`${API_BASE_URL}${url}`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    window.clearTimeout(timeoutId);
    if (error.name === "AbortError") {
      throw new Error("The request timed out. Make sure the backend is running and try again.");
    }
    throw new Error("Could not reach the server. Make sure the backend is running on port 8000.");
  }
  window.clearTimeout(timeoutId);

  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }

  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail)
      ? detail.map((e) => e.msg.replace("String should have at least", "Must be at least").replace("string_too_short", "")).join(" · ")
      : detail || "Request failed.";
    throw new Error(message);
  }
  return payload;
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  const status = document.getElementById("auth-status");
  const submitButton = document.getElementById("login-submit");

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    submitButton.textContent = "Logging in...";
    setStatus(status, "Checking your credentials...");

    try {
      const payload = await submitAuth("/auth/login", {
        email: document.getElementById("email").value,
        password: document.getElementById("password").value,
      });
      setStatus(status, payload.message, "success");
      window.setTimeout(() => {
        window.location.href = "./app.html";
      }, 700);
    } catch (error) {
      setStatus(status, error.message, "error");
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Login";
    }
  });
}

const registerForm = document.getElementById("register-form");
if (registerForm) {
  const status = document.getElementById("auth-status");
  const submitButton = document.getElementById("register-submit");

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    submitButton.textContent = "Creating...";
    setStatus(status, "Creating your account...");

    try {
      const password = document.getElementById("password").value;
      const confirmPassword = document.getElementById("confirm_password").value;
      if (password !== confirmPassword) {
        throw new Error("The passwords don't match.");
      }

      const payload = await submitAuth("/auth/register", {
        name: document.getElementById("name").value,
        email: document.getElementById("email").value,
        password,
      });
      setStatus(status, payload.message, "success");
      window.setTimeout(() => {
        sessionStorage.setItem("prefill_email", document.getElementById("email").value);
        window.location.href = "./register-success.html";
      }, 700);
    } catch (error) {
      const message = error.message.includes("already exists")
        ? "That email is already registered. Try logging in instead."
        : error.message.includes("Registration is closed")
          ? "Registration is closed on this instance — ask the operator to add your email to OPERATOR_EMAILS."
          : error.message;
      setStatus(status, message, "error");
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = "Create account";
    }
  });
}

prefillEmail();
