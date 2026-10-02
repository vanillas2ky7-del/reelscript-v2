'use strict';
const $ = id => document.getElementById(id);
const form = $('transcribeForm');
const linkTab = $('linkTab');
const fileTab = $('fileTab');
const fileInput = $('videoFile');
const dropZone = $('dropZone');
let currentInput = 'link';
let jobTimer = null;
let jobId = null;

function changeTab(type) {
  if ($('submitButton').disabled) return;
  currentInput = type;
  const isLink = type === 'link';
  linkTab.classList.toggle('active', isLink);
  fileTab.classList.toggle('active', !isLink);
  linkTab.setAttribute('aria-selected', String(isLink));
  fileTab.setAttribute('aria-selected', String(!isLink));
  $('linkPane').hidden = !isLink;
  $('filePane').hidden = isLink;
  clearError();
}
linkTab.addEventListener('click', () => changeTab('link'));
fileTab.addEventListener('click', () => changeTab('file'));
$('switchFile').addEventListener('click', () => changeTab('file'));

$('pasteButton').addEventListener('click', async () => {
  try { $('reelUrl').value = await navigator.clipboard.readText(); $('reelUrl').focus(); }
  catch { $('reelUrl').focus(); $('reelUrl').select(); }
});

function showFile(file) {
  if (!file) return;
  $('fileName').textContent = file.name;
  $('fileMeta').textContent = `${(file.size / (1024 * 1024)).toFixed(1)} MB · 업로드 준비 완료`;
}
fileInput.addEventListener('change', () => showFile(fileInput.files[0]));
['dragenter','dragover'].forEach(name => dropZone.addEventListener(name, e => { e.preventDefault(); dropZone.classList.add('dragover'); }));
['dragleave','drop'].forEach(name => dropZone.addEventListener(name, e => { e.preventDefault(); dropZone.classList.remove('dragover'); }));
dropZone.addEventListener('drop', e => {
  if (!e.dataTransfer.files.length) return;
  fileInput.files = e.dataTransfer.files;
  showFile(fileInput.files[0]);
});

document.querySelectorAll('input[name="mode"]').forEach(node => {
  node.addEventListener('change', () => {
    document.querySelectorAll('.qualityCard').forEach(label => label.classList.toggle('selected', label.querySelector('input').checked));
  });
});

function showError(message, restricted = false) {
  $('status').hidden = true;
  $('errorBox').hidden = false;
  $('errorMessage').textContent = message;
  $('errorHelp').hidden = !restricted;
  setBusy(false);
}
function clearError() { $('errorBox').hidden = true; $('errorHelp').hidden = true; }
function setBusy(busy) {
  $('submitButton').disabled = busy;
  linkTab.disabled = busy;
  fileTab.disabled = busy;
  $('submitText').textContent = busy ? '대본 추출 중…' : '나레이션 추출하기';
}
function showProgress(label, progress) {
  $('status').hidden = false;
  $('statusLabel').textContent = label;
  $('statusPercent').textContent = progress + '%';
  $('progressFill').style.width = progress + '%';
}
async function checkStatus() {
  if (!jobId) return;
  try {
    const r = await fetch(`/api/jobs/${jobId}`, {cache:'no-store'});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '작업 상태를 읽지 못했습니다.');
    showProgress(data.stage || '처리 중…', data.progress || 0);
    if (data.state === 'done') {
      stopPolling(); setBusy(false); showResult(data.result); $('status').hidden = true;
    } else if (data.state === 'failed') {
      stopPolling(); showError(data.error || '오류가 발생했습니다.', data.code === 'instagram_restricted');
    }
  } catch (error) {
    stopPolling(); showError(error.message || '서버와 연결할 수 없습니다.');
  }
}
function stopPolling() { if (jobTimer) clearInterval(jobTimer); jobTimer = null; jobId = null; }
function showResult(result) {
  $('resultSection').hidden = false;
  $('resultText').value = result.transcript || '';
  $('rawText').textContent = result.raw_transcript || '';
  $('secondPass').hidden = !result.second_transcript;
  $('secondText').textContent = result.second_transcript || '';
  $('resultMode').textContent = result.mode === 'accurate' ? '✦ 정확도 우선 · 2회 비교 완료' : '⚡ 빠르게 추출 완료';
  updateCount();
  $('resultSection').scrollIntoView({behavior:'smooth', block:'start'});
}
function updateCount(){ $('resultCount').textContent = `${$('resultText').value.length.toLocaleString('ko-KR')}자`; }
$('resultText').addEventListener('input',updateCount);

form.addEventListener('submit', async e => {
  e.preventDefault();
  clearError(); $('resultSection').hidden = true;
  const data = new FormData();
  if (currentInput === 'link') {
    const url = $('reelUrl').value.trim();
    if (!url) { showError('인스타그램 릴스 주소를 입력해주세요.'); return; }
    data.append('url', url);
  } else {
    if (!fileInput.files[0]) { showError('영상이나 음성 파일을 선택해주세요.'); return; }
    if (fileInput.files[0].size > 250 * 1024 * 1024) { showError('250MB 이하 파일을 업로드해주세요.'); return; }
    data.append('video', fileInput.files[0]);
  }
  data.append('mode', document.querySelector('input[name="mode"]:checked').value);
  data.append('hints', $('hints').value);
  setBusy(true);
  showProgress('작업 등록 및 파일 준비 중…', 0);
  try {
    const r = await fetch('/api/jobs', {method:'POST',body:data});
    const res = await r.json();
    if (!r.ok) throw new Error(typeof res.detail === 'string' ? res.detail : '요청을 처리할 수 없습니다.');
    jobId = res.job_id;
    await checkStatus();
    if (jobId) jobTimer = setInterval(checkStatus, 2300);
  } catch (error) { stopPolling(); showError(error.message || '연결 문제가 발생했습니다.'); }
});

$('copyButton').addEventListener('click', async () => {
  const text = $('resultText').value;
  try { await navigator.clipboard.writeText(text); }
  catch { $('resultText').focus(); $('resultText').select(); document.execCommand('copy'); }
  const button = $('copyButton'); button.textContent = '✓ 복사 완료';
  setTimeout(() => button.textContent = '▢ 복사하기', 1800);
});
$('downloadButton').addEventListener('click', () => {
  const blob = new Blob(['\ufeff' + $('resultText').value], {type:'text/plain;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `reelscript_${new Date().toISOString().slice(0,10)}.txt`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
});
$('newButton').addEventListener('click', () => { $('resultSection').hidden = true; clearError(); window.scrollTo({top:0,behavior:'smooth'}); });

fetch('/api/health').then(r => r.json()).then(data => {
  $('healthDot').className = 'dot ' + (data.ok && data.transcription_configured && data.ffmpeg_installed ? 'ok':'bad');
  $('healthText').textContent = !data.transcription_configured ? 'API 키 설정 필요' : !data.ffmpeg_installed ? 'FFmpeg 설정 필요' : '고정밀 인식 준비 완료';
}).catch(() => { $('healthDot').className = 'dot bad'; $('healthText').textContent = '서버 확인 실패'; });
