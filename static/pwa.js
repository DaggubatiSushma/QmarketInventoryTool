"use strict";

(() => {
  const banner = document.querySelector("#install-banner");
  const installButton = document.querySelector("#install-app");
  const dialog = document.querySelector("#install-dialog");
  const status = document.querySelector("#pwa-status");
  const standalone = window.matchMedia("(display-mode: standalone)");
  let installPrompt = null;

  function updateInstallVisibility() {
    banner.hidden = standalone.matches || navigator.standalone === true;
  }

  function updateConnection() {
    document.querySelector("#connection-status").hidden = navigator.onLine;
  }

  function showError(message, error) {
    status.textContent = message;
    status.hidden = false;
    console.warn(message, error);
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    updateInstallVisibility();
  });

  installButton.addEventListener("click", async () => {
    if (!installPrompt) {
      dialog.showModal();
      return;
    }
    const prompt = installPrompt;
    installPrompt = null;
    installButton.disabled = true;
    try {
      await prompt.prompt();
      await prompt.userChoice;
    } catch (error) {
      showError("Installation couldn't start. Use your browser's Add to Home Screen menu.", error);
      dialog.showModal();
    } finally {
      installButton.disabled = false;
    }
  });

  for (const id of ["close-install", "install-done"]) {
    document.getElementById(id).addEventListener("click", () => dialog.close());
  }
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    banner.hidden = true;
  });
  standalone.addEventListener("change", updateInstallVisibility);
  window.addEventListener("online", updateConnection);
  window.addEventListener("offline", updateConnection);
  updateInstallVisibility();
  updateConnection();

  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" }).catch((error) => {
      showError("Home-screen setup couldn't finish. Refresh to retry; the app can still be used online.", error);
    });
  }
})();
