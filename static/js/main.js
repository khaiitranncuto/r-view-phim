// =====================================================================
// GLOBAL APP STATE
// =====================================================================
const state = {
    apiKey: '',
    selectedVideo: '',
    selectedBgm: '',
    bgmVolume: 12,
    modelName: 'gemini-3.5-flash',
    script: '',
    sentences: [],
    voiceName: 'vi-VN-HoaiMyNeural',
    voiceRate: '+10%',
    projectId: '',
    scriptProjectId: '',
    scriptVideo: '',
    splitScriptSnapshot: '',
    highestStepReached: 1
};

// =====================================================================
// DOM ELEMENTS REFERENCE
// =====================================================================
const dom = {
    // Stepper
    steps: document.querySelectorAll('.step'),
    stepLines: document.querySelectorAll('.step-line'),
    panels: document.querySelectorAll('.wizard-panel'),
    
    // Step 1
    apiKeyInput: document.getElementById('api-key-input'),
    btnToggleApiKey: document.getElementById('btn-toggle-api-key'),
    modelSelect: document.getElementById('model-select'),
    btnVerifyApi: document.getElementById('btn-verify-api'),
    apiStatusBadge: document.getElementById('api-status-badge'),
    videoSelect: document.getElementById('video-select'),
    videoDropZone: document.getElementById('video-drop-zone'),
    videoFileInput: document.getElementById('video-file-input'),
    videoUploadProgress: document.getElementById('video-upload-progress'),
    btnToStep2: document.getElementById('btn-to-step2'),
    
    // Step 2
    styleSelect: document.getElementById('style-select'),
    customPrompt: document.getElementById('custom-prompt'),
    btnGenerateScript: document.getElementById('btn-generate-script'),
    scriptEditor: document.getElementById('script-editor'),
    scriptCharCount: document.getElementById('script-char-count'),
    scriptGenLoading: document.getElementById('script-gen-loading'),
    scriptLoadingTitle: document.getElementById('script-loading-title'),
    scriptProgressFill: document.getElementById('script-progress-fill'),
    scriptLoadingMsg: document.getElementById('script-loading-msg'),
    btnToStep3: document.getElementById('btn-to-step3'),
    
    // Step 3
    voiceSelect: document.getElementById('voice-select'),
    voiceRateSlider: document.getElementById('voice-rate-slider'),
    voiceRateVal: document.getElementById('voice-rate-val'),
    btnReSplit: document.getElementById('btn-re-split'),
    btnAddSentence: document.getElementById('btn-add-sentence'),
    sentencesList: document.getElementById('sentences-list'),
    btnToStep4: document.getElementById('btn-to-step4'),
    
    // Step 4
    bgmSelect: document.getElementById('bgm-select'),
    bgmVolumeSlider: document.getElementById('bgm-volume-slider'),
    bgmVolumeVal: document.getElementById('bgm-volume-val'),
    bgmDropZone: document.getElementById('bgm-drop-zone'),
    bgmFileInput: document.getElementById('bgm-file-input'),
    bgmUploadProgress: document.getElementById('bgm-upload-progress'),
    filterMirror: document.getElementById('filter-mirror'),
    filterZoom: document.getElementById('filter-zoom'),
    filterKenBurns: document.getElementById('filter-ken-burns'),
    filterContrast: document.getElementById('filter-contrast'),
    filterSpeed: document.getElementById('filter-speed'),
    subSize: document.getElementById('sub-size'),
    subColor: document.getElementById('sub-color'),
    subOpacity: document.getElementById('sub-opacity'),
    btnToStep5: document.getElementById('btn-to-step5'),
    
    // Step 5
    summaryVideo: document.getElementById('summary-video'),
    summarySentences: document.getElementById('summary-sentences'),
    summaryVoice: document.getElementById('summary-voice'),
    summaryBgm: document.getElementById('summary-bgm'),
    summaryFilters: document.getElementById('summary-filters'),
    btnStartRender: document.getElementById('btn-start-render'),
    renderProgressBox: document.getElementById('render-progress-box'),
    renderStatusLbl: document.getElementById('render-status-lbl'),
    renderStatusPercent: document.getElementById('render-status-percent'),
    renderProgressFill: document.getElementById('render-progress-fill'),
    renderLogTerminal: document.getElementById('render-log-terminal'),
    videoPreviewPlaceholder: document.getElementById('video-preview-placeholder'),
    videoPreviewWrapper: document.getElementById('video-preview-wrapper'),
    finalVideoPlayer: document.getElementById('final-video-player'),
    btnDownloadVideo: document.getElementById('btn-download-video'),
    btnRenderBack: document.getElementById('btn-render-back'),
    exportDirectoryDisplay: document.getElementById('export-directory-display'),
    btnChooseExportDirectory: document.getElementById('btn-choose-export-directory'),
    
    // Commons
    backButtons: document.querySelectorAll('.btn-back'),
    toastContainer: document.getElementById('toast-container')
};

// =====================================================================
// HELPER FUNCTIONS
// =====================================================================
function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
        <i class="fa-solid ${type === 'success' ? 'fa-circle-check text-green' : 'fa-circle-exclamation text-red'}"></i>
        <span>${message}</span>
    `;
    dom.toastContainer.appendChild(toast);
    
    // Auto remove after 4 seconds
    setTimeout(() => {
        toast.style.animation = 'slideIn 0.3s reverse forwards';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function createProjectId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
    }
    return `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function resetProjectData(nextVideo = '') {
    state.projectId = createProjectId();
    state.selectedVideo = nextVideo;
    state.script = '';
    state.sentences = [];
    state.scriptProjectId = '';
    state.scriptVideo = '';
    state.splitScriptSnapshot = '';
    state.highestStepReached = Math.min(state.highestStepReached, 2);

    if (dom.scriptEditor) dom.scriptEditor.value = '';
    if (dom.scriptCharCount) dom.scriptCharCount.textContent = '0 tá»«';
    if (dom.sentencesList) dom.sentencesList.innerHTML = '';
    if (dom.renderProgressBox) dom.renderProgressBox.classList.add('hidden');
    if (dom.renderLogTerminal) dom.renderLogTerminal.innerHTML = '';
    if (dom.videoPreviewWrapper) dom.videoPreviewWrapper.classList.add('hidden');
    if (dom.videoPreviewPlaceholder) dom.videoPreviewPlaceholder.classList.remove('hidden');
    if (dom.finalVideoPlayer) {
        dom.finalVideoPlayer.pause();
        dom.finalVideoPlayer.removeAttribute('src');
        dom.finalVideoPlayer.load();
    }
    if (dom.btnDownloadVideo) dom.btnDownloadVideo.removeAttribute('href');
    if (dom.btnStartRender) {
        dom.btnStartRender.disabled = false;
        dom.btnStartRender.innerHTML = '<i class="fa-solid fa-circle-play"></i> Báº®T Äáº¦U XUáº¤T VIDEO REVIEW';
    }
    if (dom.btnRenderBack) dom.btnRenderBack.disabled = false;
    stopRenderTimer();
}

async function loadExportDirectory() {
    try {
        const res = await fetch('/api/export-directory');
        const data = await res.json();
        if (dom.exportDirectoryDisplay) dom.exportDirectoryDisplay.value = data.directory || '';
        if (data.directory && data.valid === false && data.message) {
            showToast(data.message, 'error');
        }
    } catch (_) {
        // Export directory is optional; normal rendering can continue.
    }
}

async function chooseExportDirectory() {
    if (!dom.btnChooseExportDirectory) return;
    dom.btnChooseExportDirectory.disabled = true;
    try {
        const res = await fetch('/api/export-directory', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            dom.exportDirectoryDisplay.value = data.directory;
            showToast('ÄÃ£ lÆ°u thÆ° má»¥c xuáº¥t. Video sau khi render sáº½ tá»± chÃ©p vÃ o Ä‘Ã¢y.', 'success');
        } else if (!data.cancelled) {
            showToast(data.message || 'KhÃ´ng thá»ƒ chá»n thÆ° má»¥c xuáº¥t.', 'error');
        }
    } catch (_) {
        showToast('KhÃ´ng thá»ƒ má»Ÿ há»™p chá»n thÆ° má»¥c.', 'error');
    } finally {
        dom.btnChooseExportDirectory.disabled = false;
    }
}

function updateAPIIndicator(isValid, message) {
    const indicator = dom.apiStatusBadge.querySelector('.indicator');
    const statusText = dom.apiStatusBadge.querySelector('.status-text');
    
    if (isValid) {
        indicator.className = 'indicator green';
        statusText.textContent = 'API ÄÃ£ Káº¿t Ná»‘i';
        dom.apiStatusBadge.style.background = 'rgba(16, 185, 129, 0.08)';
        dom.apiStatusBadge.style.borderColor = 'rgba(16, 185, 129, 0.3)';
    } else {
        indicator.className = 'indicator red';
        statusText.textContent = 'ChÆ°a xÃ¡c thá»±c API';
        dom.apiStatusBadge.style.background = 'rgba(239, 68, 68, 0.08)';
        dom.apiStatusBadge.style.borderColor = 'rgba(239, 68, 68, 0.3)';
    }
}

function updateCharCount() {
    const text = dom.scriptEditor.value.trim();
    const wordCount = text ? text.split(/\s+/).filter(w => w.length > 0).length : 0;
    // TTS doc khoang 150 tu/phut
    const estMinutes = (wordCount / 150).toFixed(1);
    dom.scriptCharCount.textContent = `${wordCount} tá»« (~${estMinutes} phÃºt)`;
}

// =====================================================================
// STEP NAVIGATION LOGIC
// =====================================================================
function switchPanel(stepNum) {
    if (stepNum > state.highestStepReached) return;
    
    // Má»Ÿ Panel tÆ°Æ¡ng á»©ng
    dom.panels.forEach(p => p.classList.remove('active'));
    document.getElementById(`panel-${stepNum}`).classList.remove('hidden');
    document.getElementById(`panel-${stepNum}`).classList.add('active');
    
    // Cáº­p nháº­t Stepper CSS
    dom.steps.forEach(step => {
        const sNum = parseInt(step.getAttribute('data-step'));
        step.classList.remove('active', 'completed');
        if (sNum === stepNum) {
            step.classList.add('active');
        } else if (sNum < stepNum) {
            step.classList.add('completed');
        }
    });
    
    dom.stepLines.forEach((line, idx) => {
        line.classList.remove('completed');
        if (idx + 1 < stepNum) {
            line.classList.add('completed');
        }
    });
    
    // Má»™t sá»‘ logic chuáº©n bá»‹ khi vÃ o bÆ°á»›c cá»¥ thá»ƒ
    if (stepNum === 3) {
        // Tá»± Ä‘á»™ng phÃ¢n tÃ¡ch ká»‹ch báº£n thÃ nh cÃ¢u náº¿u cÃ¢u Ä‘ang trá»‘ng
        if (state.sentences.length === 0) {
            autoSplitScript();
        }
    } else if (stepNum === 5) {
        updateRenderSummary();
    }
}

function enableStep(stepNum) {
    if (stepNum > state.highestStepReached) {
        state.highestStepReached = stepNum;
    }
    // KÃ­ch hoáº¡t click sá»± kiá»‡n
    const stepEl = document.querySelector(`.step[data-step="${stepNum}"]`);
    if (stepEl) {
        stepEl.style.cursor = 'pointer';
    }
}

// =====================================================================
// INITIAL LOADING & API TASKS
// =====================================================================
async function loadInitialData() {
    // Load API key tu localStorage neu co
    const savedKey = localStorage.getItem('gemini_api_key');
    if (savedKey) {
        dom.apiKeyInput.value = savedKey;
    }
    state.apiKey = dom.apiKeyInput.value;
    
    // Tai danh sach video va nhac nen co san
    await loadVideosList();
    await loadBgmsList();
    
    // Tu dong kiem tra API Key neu co san
    if (state.apiKey) {
        verifyApiKey(false);
    }
}

async function verifyApiKey(showSuccessToast = true) {
    state.apiKey = dom.apiKeyInput.value.trim();
    if (!state.apiKey) {
        showToast('Vui lÃ²ng nháº­p API Key!', 'error');
        return;
    }
    
    dom.btnVerifyApi.disabled = true;
    dom.btnVerifyApi.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Äang xÃ¡c thá»±c...';
    
    try {
        const res = await fetch('/api/check-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: state.apiKey })
        });
        const data = await res.json();
        
        if (data.valid) {
            updateAPIIndicator(true);
            // Luu API key vao localStorage
            localStorage.setItem('gemini_api_key', state.apiKey);
            if (showSuccessToast) {
                showToast('Káº¿t ná»‘i API Gemini thÃ nh cÃ´ng! Key Ä‘Ã£ Ä‘Æ°á»£c ghi nhá»›.', 'success');
            }
            
            // Auto-populate model dropdown tu API
            if (data.models && data.models.length > 0) {
                const modelSelect = dom.modelSelect;
                const currentVal = modelSelect.value;
                modelSelect.innerHTML = '';
                
                // Uu tien lite models (an toan nhat cho free tier), sau do flash
                const preferred = ['gemini-3.1-flash-lite', 'gemini-2.0-flash-lite', 'gemini-2.5-flash-lite', 'gemini-3-flash-preview'];
                const sorted = data.models.sort((a, b) => {
                    const aIdx = preferred.indexOf(a);
                    const bIdx = preferred.indexOf(b);
                    if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
                    if (aIdx !== -1) return -1;
                    if (bIdx !== -1) return 1;
                    return a.localeCompare(b);
                });
                
                sorted.forEach((m, i) => {
                    const opt = document.createElement('option');
                    opt.value = m;
                    let label = m;
                    if (m.includes('flash-lite')) label += ' (Nháº¹, nhanh)';
                    else if (m.includes('flash')) label += ' (Nhanh)';
                    else if (m.includes('pro')) label += ' (ThÃ´ng minh)';
                    if (i === 0) label += ' â­ KhuyÃªn dÃ¹ng';
                    opt.textContent = label;
                    if (m === currentVal) opt.selected = true;
                    modelSelect.appendChild(opt);
                });
                
                // Neu ko co current value, chon cai dau tien
                if (!currentVal || !sorted.includes(currentVal)) {
                    modelSelect.selectedIndex = 0;
                }
                console.log(`[+] Loaded ${sorted.length} models from API`);
            }
            
            checkNextStep1Availability();
        } else {
            updateAPIIndicator(false);
            showToast(data.message, 'error');
        }
    } catch (err) {
        upda×NuêÚ$z{-®éÜj×†–v†W7E7FW&V6†VB’°¢7v—F6…æVÂ‡7FWçVÒ“°¢Ğ¢Ò“°¢Ò“°¢ ¢òòV’Îª¢FöÒæ&6´'WGFöç2æf÷$V6‚†'FâÓâ°¢'FâæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7BF&vWBÒ'6T–çB†'FâævWDGG&–'WFR‚vFF×F&vWBr’“°¢7v—F6…æVÂ‡F&vWB“°¢Ò“°¢Ò“°¢ ¢òòFövvÆR†¸6âF¸²’¶W¢FöÒæ'FåFövvÆT”¶W’æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7BG—RÒFöÒæ”¶W”–çWBçG—RÓÓÒw77v÷&BròwFW‡Br¢w77v÷&Bs°¢FöÒæ”¶W”–çWBçG—RÒG—S°¢FöÒæ'FåFövvÆT”¶W’çVW'•6VÆV7F÷"‚v’r’æ6Æ74æÖRÒG—RÓÓÒw77v÷&Bròvf×6öÆ–BfÖW–Rr¢vf×6öÆ–BfÖW–R×6Æ6‚s°¢Ò“°¢ ¢òò’fW&–f–6F–öâ'WGFöà¢FöÒæ'FåfW&–g”’æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’ÓâfW&–g””¶W’‡G'VR’“°¢ ¢òò6†V6²¶W’öâFW‡B–çWB6†ævP¢FöÒæ”¶W”–çWBæFDWfVçDÆ—7FVæW"‚v–çWBrÂ‚’Óâ°¢6†V6´æW‡E7FWf–Æ&–Æ—G’‚“°¢Ò“°¢ ¢òò6VÆV7Bf–FVò–çWB6†ævP¢FöÒçf–FVõ6VÆV7BæFDWfVçDÆ—7FVæW"‚v6†ævRrÂ‚’Óâ°¢6öç7BæW‡Ef–FVòÒFöÒçf–FVõ6VÆV7BçfÇVS°¢–b†æW‡Ef–FVòÓÒ7FFRç6VÆV7FVEf–FVò’&W6WE&ö¦V7DFF†æW‡Ef–FVò“°¢7FFRç6VÆV7FVEf–FVòÒæW‡Ef–FVó°¢6†V6´æW‡E7FWf–Æ&–Æ—G’‚“°¢Ò“°¢ ¢òòG&rôG&÷f–FVòWÆöB6WGW ¢6WGWG&tæDG&÷†FöÒçf–FVôG&÷¦öæRÂFöÒçf–FVôf–ÆT–çWBÂFöÒçf–FVõWÆöE&öw&W72Âwf–FVòrÂ†f–ÆVæÖR’Óâ°¢òòNª6’Îª’Æ—7Bl:6¸Öâf–FVòŞ¹¶’WÆö@¢ÆöEf–FV÷4Æ—7B‚’çF†Vâ‚‚’Óâ°¢FöÒçf–FVõ6VÆV7BçfÇVRÒf–ÆVæÖS°¢&W6WE&ö¦V7DFF†f–ÆVæÖR“°¢6†V6´æW‡E7FWf–Æ&–Æ—G’‚“°¢Ò“°¢Ò“°¢ ¢òò'WGFöâ7FWÓâ7FW ¢FöÒæ'FåFõ7FW"æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ7v—F6…æVÂƒ"’“°¢ ¢òò’67&—Gw&—FW"'WGFöà¢FöÒæ'FävVæW&FU67&—BæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂvVæW&FU67&—B“°¢ ¢òò67&—BVF—F÷"6†ævW0¢FöÒç67&—DVF—F÷"æFDWfVçDÆ—7FVæW"‚v–çWBrÂWFFT6†$6÷VçB“°¢ ¢òò'WGFöâ7FW"Óâ7FW0¢FöÒæ'FåFõ7FW2æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢òòÌkRvœ:G.¸²¾¸¶6‚.ª6â7^¹’<;–ærl:6‡W¸6â,k¹¶0¢7FFRç67&—BÒFöÒç67&—DVF—F÷"çfÇVRçG&–Ò‚“°¢–b‚7FFRç67&—B’°¢6†÷uFö7B‚tî¹–’GVær¾¸¶6‚.ª6âG.¹ærrÂvW'&÷"r“°¢&WGW&ã°¢Ğ¢–b‡7FFRç67&—BÓÒ7FFRç7Æ—E67&—E6æ6†÷B’°¢7FFRç6VçFVæ6W2ÒµÓ°¢Ğ¢7v—F6…æVÂƒ2“°¢Ò“°¢ ¢òò&R×7Æ—B6VçFVæ6W2'WGFöà¢FöÒæ'Få&U7Æ—BæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂWFõ7Æ—E67&—B“°¢ ¢òòFBæWr6VçFVæ6R'WGFöà¢FöÒæ'FäFE6VçFVæ6RæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢7FFRç6VçFVæ6W2çW6‚‚r„æª×<:'RŞ¹¶’Nª’I:'’’r“°¢&VæFW%6VçFVæ6W2‚“°¢WFFU6VçFVæ6U7FG2‚“°¢òò67&öÆÂFò&÷GFöĞ¢FöÒç6VçFVæ6W4Æ—7Bç67&öÆÅF÷ÒFöÒç6VçFVæ6W4Æ—7Bç67&öÆÄ†V–v‡C°¢6†÷uFö7B‚|I:2FŒ:¦Ò<:'RŞ¹¶’rÂw7V66W72r“°¢Ò“°¢ ¢òòfö–6R6Æ–FW'2F—7Æ’7–æ0¢FöÒçfö–6U&FU6Æ–FW"æFDWfVçDÆ—7FVæW"‚v–çWBrÂ‚’Óâ°¢6öç7BfÂÒ'6T–çB†FöÒçfö–6U&FU6Æ–FW"çfÇVR“°¢FöÒçfö–6U&FUfÂçFW‡D6öçFVçBÒG·fÂãÒòr²r¢rwÒG·fÇÒV°¢Ò“°¢ ¢òò'WGFöâ7FW2Óâ7FW@¢FöÒæ'FåFõ7FWBæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢òòIª6Ò.ª6òÎªW’Fæ‚<:6‚<:'RI:26¸–æ‚>ºÖ¢6öç7BFW‡F&V2ÒFöÒç6VçFVæ6W4Æ—7BçVW'•6VÆV7F÷$ÆÂ‚rç6VçFVæ6R×FW‡F&Vr“°¢6öç7BWFFVBÒµÓ°¢FW‡F&V2æf÷$V6‚‡FÓâ°¢–b‡FçfÇVRçG&–Ò‚’’WFFVBçW6‚‡FçfÇVRçG&–Ò‚’“°¢Ò“°¢ ¢–b‡WFFVBæÆVæwF‚ÓÓÒ’°¢6†÷uFö7B‚ugV’Ì;&ærNªò¾¸¶6‚.ª6âl:Œ:&â<:'RrÂvW'&÷"r“°¢&WGW&ã°¢Ğ¢7FFRç6VçFVæ6W2ÒWFFVC°¢7v—F6…æVÂƒB“°¢WFFU7V'F—FÆU&Wf–Wt&6¶w&÷VæB‡7FFRç6VÆV7FVEf–FVò“°¢Ò“°¢ ¢òò7R¶–VâFövvÆR†–VâF†’‡RFP¢6öç7BFövvÆU7V'F—FÆW2ÒFö7VÖVçBævWDVÆVÖVçD'”–B‚vVæ&ÆR×7V'F—FÆW2r“°¢–b‡FövvÆU7V'F—FÆW2’°¢FövvÆU7V'F—FÆW2æFDWfVçDÆ—7FVæW"‚v6†ævRrÂ†R’Óâ°¢6öç7B&Wf–Wt&÷‚ÒFö7VÖVçBævWDVÆVÖVçD'”–B‚w7V'F—FÆR×&Wf–WrÖ&÷‚r“°¢–b†RçF&vWBæ6†V6¶VB’°¢–b‡&Wf–Wt&÷‚’&Wf–Wt&÷‚ç7G–ÆRæ÷6—G’Òss°¢FöÒç7V%6—¦RæF—6&ÆVBÒfÇ6S°¢FöÒç7V$6öÆ÷"æF—6&ÆVBÒfÇ6S°¢ÒVÇ6R°¢–b‡&Wf–Wt&÷‚’&Wf–Wt&÷‚ç7G–ÆRæ÷6—G’Òsã2s°¢FöÒç7V%6—¦RæF—6&ÆVBÒG'VS°¢FöÒç7V$6öÆ÷"æF—6&ÆVBÒG'VS°¢Ğ¢Ò“°¢Ğ¢ ¢òòG&rôG&÷$tÒWÆöB6WGW ¢6WGWG&tæDG&÷†FöÒæ&vÔG&÷¦öæRÂFöÒæ&vÔf–ÆT–çWBÂFöÒæ&vÕWÆöE&öw&W72Âv&vÒrÂ†f–ÆVæÖR’Óâ°¢ÆöD&v×4Æ—7B‚’çF†Vâ‚‚’Óâ°¢FöÒæ&vÕ6VÆV7BçfÇVRÒf–ÆVæÖS°¢Ò“°¢Ò“°¢ ¢òò$tÒföÇVÖR6Æ–FW"7–æ0¢FöÒæ&vÕföÇVÖU6Æ–FW"æFDWfVçDÆ—7FVæW"‚v–çWBrÂ‚’Óâ°¢FöÒæ&vÕföÇVÖUfÂçFW‡D6öçFVçBÒG¶FöÒæ&vÕföÇVÖU6Æ–FW"çfÇVWÒV°¢Ò“°¢ ¢òòÓÓÓÓÒ7V'F—FÆRÆ—fR&Wf–WrÓÓÓÓĞ¢gVæ7F–öâWFFU7V'F—FÆU&Wf–Wr‚’°¢6öç7B&Wf–WuFW‡BÒFö7VÖVçBævWDVÆVÖVçD'”–B‚w&Wf–Wr×7V'F—FÆR×FW‡Br“°¢–b‚&Wf–WuFW‡B’&WGW&ã°¢ ¢6öç7B6—¦RÒ'6T–çB†FöÒç7V%6—¦RçfÇVR’ÇÂC#°¢6öç7B6öÆ÷"ÒFöÒç7V$6öÆ÷"çfÇVRÇÂr4dddddbs°¢6öç7B÷6—G’Ò'6T–çB†FöÒç7V$÷6—G’çfÇVR’ÇÂS°¢ ¢òòF–æ‚Föâæ‡R&6¶VæC¢52föçG6—¦RÒW6W%÷‚¢3ƒBòƒ ¢òò&Wf–Wr66ÆS¢&Wf–Wr&÷‚ã#‚6òÂf–FVòF†Bƒ‚ÓâG’ÆRããƒP¢òòÓâ&Wf–Wu÷‚ÒW6W%÷‚¢ƒ3ƒBóƒ’¢ƒ#ó3ƒB’ÒW6W%÷‚¢#óƒ(˜‚W6W%÷‚¢ãƒP¢6öç7B66ÆVE6—¦RÒÖF‚æÖ‚ƒ‚ÂÖF‚ç&÷VæB‡6—¦R¢ãƒR’“°¢6öç7B&tÇ†Ò†÷6—G’ò#SR’çFôf—†VBƒ"“°¢ ¢&Wf–WuFW‡Bç7G–ÆRæföçE6—¦RÒG·66ÆVE6—¦W×†°¢&Wf–WuFW‡Bç7G–ÆRæ6öÆ÷"Ò6öÆ÷#°¢&Wf–WuFW‡Bç7G–ÆRæ&6¶w&÷VæBÒ&v&ƒÂÂÂG¶&tÇ†Ò–°¢Ğ¢ ¢FöÒç7V%6—¦RæFDWfVçDÆ—7FVæW"‚v–çWBrÂWFFU7V'F—FÆU&Wf–Wr“°¢FöÒç7V$6öÆ÷"æFDWfVçDÆ—7FVæW"‚v–çWBrÂWFFU7V'F—FÆU&Wf–Wr“°¢FöÒç7V$÷6—G’æFDWfVçDÆ—7FVæW"‚v–çWBrÂWFFU7V'F—FÆU&Wf–Wr“°¢ ¢òò7V'F—FÆR&W6WB6†—0¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rç&W6WBÖ6†—r’æf÷$V6‚†6†—Óâ°¢6†—æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7B6—¦RÒ6†—ævWDGG&–'WFR‚vFF×6—¦Rr“°¢6öç7B6öÆ÷"Ò6†—ævWDGG&–'WFR‚vFFÖ6öÆ÷"r“°¢6öç7B÷6—G’Ò6†—ævWDGG&–'WFR‚vFFÖ÷6—G’r“°¢ ¢FöÒç7V%6—¦RçfÇVRÒ6—¦S°¢FöÒç7V$6öÆ÷"çfÇVRÒ6öÆ÷#°¢FöÒç7V$÷6—G’çfÇVRÒ÷6—G“°¢ ¢òò6æ†B7F—fR7FFP¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rç&W6WBÖ6†—r’æf÷$V6‚†2Óâ2æ6Æ74Æ—7Bç&VÖ÷fR‚v7F—fRr’“°¢6†—æ6Æ74Æ—7BæFB‚v7F—fRr“°¢ ¢WFFU7V'F—FÆU&Wf–Wr‚“°¢6†÷uFö7B‚|I:2:NºVærŞª·RºRI¸rÂw7V66W72r“°¢Ò“°¢Ò“°¢ ¢òò–æ—F–Â&Wf–Wp¢WFFU7V'F—FÆU&Wf–Wr‚“°¢ ¢òò'WGFöâ7FWBÓâ7FWP¢FöÒæ'FåFõ7FWRæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢Væ&ÆU7FWƒR“°¢7v—F6…æVÂƒR“°¢Ò“°¢ ¢òò7F'B&VæFW&–ær'WGFöà¢FöÒæ'Få7F'E&VæFW"æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ7F'E&VæFW%f–FVò“°¢–b†FöÒæ'Fä6†ö÷6TW‡÷'DF—&V7F÷'’’°¢FöÒæ'Fä6†ö÷6TW‡÷'DF—&V7F÷'’æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ6†ö÷6TW‡÷'DF—&V7F÷'’“°¢Ğ§Ğ ¢òòÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞ¢òòDô5TÔTåBôâ$TE¢òòÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞ¦Fö7VÖVçBæFDWfVçDÆ—7FVæW"‚tDôÔ6öçFVçDÆöFVBrÂ‚’Óâ°¢ÆöD–æ—F–ÄFF‚“°¢ÆöE6g„Æ—7B‚“°¢ÆöDW‡÷'DF—&V7F÷'’‚“°¢6WGWWfVçDÆ—7FVæW'2‚“°¢ ¢òò&ö×BFV×ÆFR6†—2Òæ†âFRF–Vâfòò%–WR6R6†’F–WB ¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚rç&ö×BÖ6†—r’æf÷$V6‚†6†—Óâ°¢6†—æFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7B&ö×EFW‡BÒ6†—ævWDGG&–'WFR‚vFF×&ö×Br“°¢–b‡&ö×EFW‡BbbFöÒæ7W7FöÕ&ö×B’°¢–b†FöÒæ7W7FöÕ&ö×BçfÇVRçG&–Ò‚’ÓÒrr’°¢FöÒæ7W7FöÕ&ö×BçfÇVR³ÒuÆâÒr²&ö×EFW‡C°¢ÒVÇ6R°¢FöÒæ7W7FöÕ&ö×BçfÇVRÒ&ö×EFW‡C°¢Ğ¢FöÒæ7W7FöÕ&ö×Bæfö7W2‚“°¢òò†–WRVærf—7VÃ¢6†—6ærÆVà¢6†—ç7G–ÆRæ&÷&FW$6öÆ÷"Òwf"‚ÒÖ66VçBÖw&VVâ’s°¢6†—ç7G–ÆRæ6öÆ÷"Òwf"‚ÒÖ66VçBÖw&VVâ’s°¢6WEF–ÖV÷WB‚‚’Óâ°¢6†—ç7G–ÆRæ&÷&FW$6öÆ÷"Òrs°¢6†—ç7G–ÆRæ6öÆ÷"Òrs°¢ÒÂƒ“°¢6†÷uFö7B‚|I:2I¸âŞª·R&ö×BrÂw7V66W72r“°¢Ğ¢Ò“°¢Ò“°¢ ¢òò.ª÷B>»¶¸vâ6†ò<:2F«²>ª6ÒŒ;¦0¢Fö7VÖVçBçVW'•6VÆV7F÷$ÆÂ‚ræVÖ÷F–öâ×Frr’æf÷$V6‚‡FrÓâ°¢FræFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7B–ç6W'EFW‡BÒFrævWDGG&–'WFR‚vFFÖ–ç6W'Br“°¢–b†–ç6W'EFW‡BbbFöÒæ7W7FöÕ&ö×B’°¢–b†FöÒæ7W7FöÕ&ö×BçfÇVRçG&–Ò‚’ÓÒrr’°¢FöÒæ7W7FöÕ&ö×BçfÇVR³ÒuÆâÒ´>ª6ÒŒ;¦5Ó¢r²–ç6W'EFW‡C°¢ÒVÇ6R°¢FöÒæ7W7FöÕ&ö×BçfÇVRÒrÒ´>ª6ÒŒ;¦5Ó¢r²–ç6W'EFW‡C°¢Ğ¢FöÒæ7W7FöÕ&ö×Bæfö7W2‚“°¢6†÷uFö7B‚|I:26Œ:†âÎ¸væ‚>ª6ÒŒ;¦2rÂw7V66W72r“°¢Ğ¢Ò“°¢Ò“° ¢òò&B7R¶–Vâvö’’æ†2æVà¢6öç7B'Få7VvvW7D&vÒÒFö7VÖVçBævWDVÆVÖVçD'”–B‚v'Fâ×7VvvW7BÖ&vÒr“°¢–b†'Få7VvvW7D&vÒ’°¢'Få7VvvW7D&vÒæFDWfVçDÆ—7FVæW"‚v6Æ–6²rÂ‚’Óâ°¢6öç7B7G–ÆRÒFöÒç7G–ÆU6VÆV7BòFöÒç7G–ÆU6VÆV7BçfÇVR¢"#°¢6öç7B÷F–öç2Ò'&’æg&öÒ†FöÒæ&vÕ6VÆV7Bæ÷F–öç2“°¢ ¢ÆWBF&vWD¶W—v÷&G2ÒµÓ°¢–b‡7G–ÆRÓÓÒ&G&ÖF–2"’F&vWD¶W—v÷&G2Ò²&W–2"Â&&GFÆR"Â&7F–öâ"Â&6–æVÖF–2"Â&G&ÖF–2%Ó°¢VÇ6R–b‡7G–ÆRÓÓÒ&‡VÖ÷&÷W2"’F&vWD¶W—v÷&G2Ò²&gVæç’"Â'6æV·’"Â&6Æ÷vâ"Â'V—&·’"Â&6öÖVG’%Ó°¢VÇ6R–b‡7G–ÆRÓÓÒ'6†÷'G2"’F&vWD¶W—v÷&G2Ò²&W–2"Â&7F–öâ"Â'7W7Vç6R"Â&7–&W'Væ²%Ó°¢VÇ6R–b‡7G–ÆRÓÓÒ&æWw2"’F&vWD¶W—v÷&G2Ò²&æWw2"Â&6÷'÷&FR"Â'W&VB"Â&VÆV7G&öæ–2%Ó°¢VÇ6R–b‡7G–ÆRÓÓÒ'GWF÷&–Â"’F&vWD¶W—v÷&G2Ò²&Æöf’"Â&6†–ÆÂ"Â'GWF÷&–Â"Â&Ö&–VçB%Ó°¢ ¢ÆWBf÷VæBÒfÇ6S°¢f÷"†ÆWB’Ò²’Â÷F–öç2æÆVæwFƒ²’²²’°¢ÆWBfÂÒ÷F–öç5¶•ÒçfÇVRçFôÆ÷vW$66R‚“°¢–b‡fÂbbF&vWD¶W—v÷&G2ç6öÖR†·rÓâfÂæ–æ6ÇVFW2†·r’’’°¢FöÒæ&vÕ6VÆV7Bç6VÆV7FVD–æFW‚Ò“°¢f÷VæBÒG'VS°¢òò¶–6‚†öBWfVçB6†ævP¢FöÒæ&vÕ6VÆV7BæF—7F6„WfVçB†æWrWfVçB‚v6†ævRr’“°¢'&V³°¢Ğ¢Ğ¢ ¢–b†f÷VæB’°¢6†÷uFö7B†I:2N»I¹–ær6¸Öâæª2î¸â6†ò†öær<:6ƒ¢G·7G–ÆWÖÂw7V66W72r“°¢ÒVÇ6R°¢6†÷uFö7B‚t¶Œ;FærL:ÆÒFªW’æª2Œ;’º7Œ:7’Nª6’<:2f–ÆRæª2<;2L:¦âæŒkW–2ÂgVæç’ÂÆöf’ââârÂwv&æ–ærr“°¢Ğ¢Ò“°¢Ğ§Ò“°  ¢òò6çf27V'F—FÆR&Wf–Wr…7FWB¦gVæ7F–öâWFFU7V'F—FÆU&Wf–Wt&6¶w&÷VæB‡f–FVôf–ÆVæÖR’°¢6öç7B&Wf–Wt&÷‚ÒFö7VÖVçBævWDVÆVÖVçD'”–B‚w7V'F—FÆR×&Wf–WrÖ&÷‚r“°¢–b‚&Wf–Wt&÷‚ÇÂf–FVôf–ÆVæÖR’&WGW&ã° ¢òòNªòŞ¹—Bf–FVòFrª–âG&öær.¹’æ¹²I¸2ÆöBf–FVò~¹0¢6öç7BFV×f–FVòÒFö7VÖVçBæ7&VFTVÆVÖVçB‚wf–FVòr“°¢FV×f–FVòæ7&÷74÷&–v–âÒ&æöç–Ö÷W2#°¢FV×f–FVòç7&2Ò÷7FF–2ö÷WGWBòG·f–FVôf–ÆVæÖRç&WÆ6R‚ræ×BrÂuövVÖ–æ•÷FV×æ×Br—Ö°¢ ¢òòfÆÆ&6²æWR6ö×&W76VB6‡V6ğ¢FV×f–FVòæöæW'&÷"ÒgVæ7F–öâ‚’°¢FV×f–FVòç7&2Ò÷7FF–2÷f–FV÷2òG·f–FVôf–ÆVæÖWÖ² ¢Ó° ¢FV×f–FVòæ7W'&VçEF–ÖRÒ"ã²òò6VV²I«öâvœ:'’Fº’"I¸2ÎªW’Œ:Ææ€ ¢FV×f–FVòæFDWfVçDÆ—7FVæW"‚vÆöFVFFFrÂ‚’Óâ°¢òòL;–ær6çf26ºWÎª’g&ÖRŒ:Ææ‚¹òvœ:'’Fº’ ¢6öç7B6çf2ÒFö7VÖVçBæ7&VFTVÆVÖVçB‚v6çf2r“°¢6çf2çv–GF‚ÒFV×f–FVòçf–FVõv–GF‚ÇÂ#ƒ°¢6çf2æ†V–v‡BÒFV×f–FVòçf–FVô†V–v‡BÇÂs#°¢6öç7B7G‚Ò6çf2ævWD6öçFW‡B‚s&Br“°¢7G‚æG&t–ÖvR‡FV×f–FVòÂÂÂ6çf2çv–GF‚Â6çf2æ†V–v‡B“° ¢òò&«öâg&ÖRŒ:Ææ‚FŒ:æ‚ª6æ‚î¸â6†ò¹—&Wf–WrºRI¸¢6öç7Bg&ÖTFFW&ÂÒ6çf2çFôFFU$Â‚v–ÖvRö§Vrr“°¢&Wf–Wt&÷‚ç7G–ÆRæ&6¶w&÷VæD–ÖvRÒW&Â‚G¶g&ÖTFFW&ÇÒ–°¢&Wf–Wt&÷‚ç7G–ÆRæ&6¶w&÷VæE6—¦RÒ&6÷fW"#°¢&Wf–Wt&÷‚ç7G–ÆRæ&6¶w&÷VæE÷6—F–öâÒ&6VçFW"#°¢&Wf–Wt&÷‚ç7G–ÆRæ&÷…6†F÷rÒ&–ç6WB‚&v&ƒÃÃÃãb’#°¢ ¢òò~º–6öâf–ÆÒI¢6öç7B–6öâÒ&Wf–Wt&÷‚çVW'•6VÆV7F÷"‚rç&Wf–WrÖ&rÖ–6öâr“°¢–b†–6öâ’–6öâç7G–ÆRæ÷6—G’Ò#ã#°¢Ò“°§Ğ ¢òòÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞ¢òòdô”4R$Ud”UrÒæv†RF‡Rv–öærFö0¢òòÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓÓĞ¢†gVæ7F–öâ‚’°¢6öç7B'FâÒFö7VÖVçBævWDVÆVÖVçD'”–B‚v'Fâ×&Wf–Wr×fö–6Rr“°¢–b‚'Fâ’&WGW&ã°¢ ¢6öç7BVF–òÒFö7VÖVçBævWDVÆVÖVçD'”–B‚wfö–6R×&Wf–WrÖVF–òr“°¢6öç7B7FGW2ÒFö7VÖVçBævWDVÆVÖVçD'”–B‚wfö–6R×&Wf–Wr×7FGW2r“°¢6öç7Bfö–6U6VÆV7BÒFö7VÖVçBævWDVÆVÖVçD'”–B‚wfö–6R×6VÆV7Br“°¢6öç7B&FU6Æ–FW"ÒFö7VÖVçBævWDVÆVÖVçD'”–B‚wfö–6R×&FR×6Æ–FW"r“°¢ ¢6öç7B6×ÆUFW‡BÒ%G&öærF«òv¹¶’Iªw’,:Òª–âì:’ÂŞ¹—B7^¹–2†œ:§RÌkRŞ¹¶’Iær.ª÷BIªwRâÆ¸wRæŒ:&ânª×B6Œ:Öæ‚<;2F¸2lkº7BVŞ¸Ö’FºÒFŒ:6‚l:L:ÆÒ&>»Fª×Cò#°¢ ¢'Fâæöæ6Æ–6²Ò7–æ2‚’Óâ°¢6öç7Bfö–6RÒfö–6U6VÆV7BçfÇVS°¢6öç7B&FUfÂÒ'6T–çB‡&FU6Æ–FW#òçfÇVRÇÂ“°¢6öç7B&FRÒ‡&FUfÂãÒòr²r¢rr’²&FUfÂ²rRs°¢ ¢òòT’ÆöF–æp¢'FâæF—6&ÆVBÒG'VS°¢'Fâæ–ææW$…DÔÂÒsÆ’6Æ73Ò&f×6öÆ–Bf×7–ææW"f×7–â#ãÂö“âIærNªòâââs°¢7FGW2ç7G–ÆRæF—7Æ’Òv–æÆ–æRs°¢7FGW2çFW‡D6öçFVçBÒrs°¢VF–òç7G–ÆRæF—7Æ’ÒvæöæRs°¢VF–òçW6R‚“°¢ ¢G'’°¢6öç7B&W2Òv—BfWF6‚‚rö’÷&Wf–Wr×GG2rÂ°¢ÖWF†öC¢uõ5BrÀ¢†VFW'3¢²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öârÒÀ¢&öG“¢¥4ôâç7G&–æv–g’‡²FW‡C¢6×ÆUFW‡BÂfö–6RÂ&FRÒ¢Ò“°¢6öç7BFFÒv—B&W2æ§6öâ‚“°¢ ¢–b†FFç7V66W72’°¢VF–òç7&2ÒFFçW&Â²s÷CÒr²FFRææ÷r‚“°¢VF–òç7G–ÆRæF—7Æ’Òv&Æö6²s°¢VF–òçÆ’‚“°¢7FGW2çFW‡D6öçFVçBÒ~)ÈRIærŒ:Bs°¢7FGW2ç7G–ÆRæ6öÆ÷"Òr3FFSƒs°¢ÒVÇ6R°¢7FGW2çFW‡D6öçFVçBÒ~)ØÂr²†FFæÖW76vRÇÂtÎ¹v’r“°¢7FGW2ç7G–ÆRæ6öÆ÷"Òr6cƒsss°¢Ğ¢Ò6F6‚†R’°¢7FGW2çFW‡D6öçFVçBÒ~)ØÂÎ¹v’¾«÷Bî¹’s°¢7FGW2ç7G–ÆRæ6öÆ÷"Òr6cƒsss°¢Ğ¢ ¢'FâæF—6&ÆVBÒfÇ6S°¢'Fâæ–ææW$…DÔÂÒsÆ’6Æ73Ò&f×6öÆ–BfÖ†VG†öæW2#ãÂö“âæv†RFºÒv¸Öærs°¢ ¢6WEF–ÖV÷WB‚‚’Óâ²7FGW2ç7G–ÆRæF—7Æ’ÒvæöæRs²ÒÂS“°¢Ó°§Ò’‚“°