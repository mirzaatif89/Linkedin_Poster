const form = document.getElementById("search-form");
const logEl = document.getElementById("log");
const statusEl = document.getElementById("status");
const submitBtn = document.getElementById("submit");
const resultPath = document.getElementById("result-path");
const resumeBtn = document.getElementById("resume");
const tabScraper = document.getElementById("tabScraper");
const tabPoster = document.getElementById("tabPoster");
const scraperView = document.getElementById("scraperView");
const posterView = document.getElementById("posterView");
const posterLoginBtn = document.getElementById("posterLogin");
const posterEmail = document.getElementById("posterEmail");
const posterPassword = document.getElementById("posterPassword");
const posterTitle = document.getElementById("posterTitle");
const postCounter = document.getElementById("postCounter");
const postTimer = document.getElementById("postTimer");
const postSchedule = document.getElementById("postSchedule");
const posterStatus = document.getElementById("posterStatus");
let poller = null;

function appendLogs(logs) {
  logEl.textContent = logs.join("\n");
  logEl.scrollTop = logEl.scrollHeight;
}

async function pollProgress() {
  try {
    const data = await window.pywebview.api.get_progress();
    appendLogs(data.logs);
    statusEl.textContent = data.status;
    statusEl.dataset.state = data.status;
    toggleResume(data.status === "verification");
    if (data.output_path) {
      resultPath.textContent = `Saved: ${data.output_path}`;
    }
    if (data.status === "idle" || data.status === "error") {
      submitBtn.disabled = false;
      if (poller) clearInterval(poller);
      poller = null;
    }
  } catch (err) {
    appendLogs([`UI error: ${err}`]);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(form);
  const payload = Object.fromEntries(formData.entries());
  submitBtn.disabled = true;
  statusEl.textContent = "running";
  statusEl.dataset.state = "running";
  resultPath.textContent = "";
  appendLogs(["Starting scrape..."]);
  try {
    await window.pywebview.api.start_scrape(payload);
    if (!poller) poller = setInterval(pollProgress, 1200);
  } catch (err) {
    appendLogs([`Start error: ${err}`]);
    submitBtn.disabled = false;
    statusEl.textContent = "error";
    statusEl.dataset.state = "error";
  }
});

function toggleResume(show) {
  if (!resumeBtn) return;
  resumeBtn.style.display = show ? "inline-flex" : "none";
}

resumeBtn?.addEventListener("click", async () => {
  resumeBtn.disabled = true;
  appendLogs([...logEl.textContent.split("\n"), "Continuing after manual verification..."]);
  try {
    await window.pywebview.api.resume_after_verification();
  } finally {
    resumeBtn.disabled = false;
  }
});

function setActiveView(view) {
  const isScraper = view === "scraper";
  scraperView.style.display = isScraper ? "block" : "none";
  posterView.style.display = isScraper ? "none" : "block";
  tabScraper.classList.toggle("active", isScraper);
  tabPoster.classList.toggle("active", !isScraper);
}

tabScraper?.addEventListener("click", () => setActiveView("scraper"));
tabPoster?.addEventListener("click", () => setActiveView("poster"));

setActiveView("scraper");

posterLoginBtn?.addEventListener("click", async () => {
  const email = posterEmail?.value?.trim() || "";
  const password = posterPassword?.value || "";
  const title = posterTitle?.value?.trim() || "";
  const counter = postCounter?.value || "1";
  const timer = postTimer?.value || "";
  const schedule = postSchedule?.value || "";
  if (!title) {
    if (posterStatus) posterStatus.textContent = "Post Category required.";
    return;
  }
  if (posterStatus) posterStatus.textContent = "Generating and posting...";
  appendLogs([...logEl.textContent.split("\n"), "Poster generate & post started..."]);
  posterLoginBtn.disabled = true;
  try {
    await window.pywebview.api.poster_generate_and_post({
      email,
      password,
      title,
      counter,
      timer,
      schedule,
    });
    if (posterStatus) posterStatus.textContent = "Post request sent. Check LinkedIn.";
  } catch (err) {
    appendLogs([...logEl.textContent.split("\n"), `Poster post error: ${err}`]);
    if (posterStatus) posterStatus.textContent = "Post error. See logs.";
  } finally {
    posterLoginBtn.disabled = false;
  }
});
