'use strict';

const state = {
  bootstrap: null,
  conversation: null,
  selected: {kind: 'version', version: null},
  run: null,
  runEvents: [],
  runEventAfter: 0,
  runTimer: null,
  runPollEpoch: 0,
  runPollInFlight: false,
  runUi: freshRunUiState(),
  activeView: 'landing',
  viewingRunContract: false,
  generationTimer: null,
  generationConversationId: null,
  generationElapsedTimer: null,
  generationElapsedAttemptId: null,
  generationElapsedBaseSeconds: 0,
  generationElapsedBaseAt: 0,
  generationNoticeClosed: false,
  researchOverviewTimer: null,
  researchOverviewFingerprint: '',
  request: {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set(), lastSavedFingerprint: null, pendingUploads: 0},
  fileLibrary: {pendingUploads: 0},
  requestEditor: {key: null, previewTimer: null, syncing: false},
  section: {key: null, previewTimer: null, saveTimer: null, syncing: false, splitRatio: .5, savePromise: null, saveQueued: false, lastSavedContent: null, openingContent: null, wholeSource: null, fragment: null, baseVersion: null, refreshAfterClose: false},
  managingProjectId: null,
  managingConversationId: null,
  managingContractVersion: null,
  archivingConversationId: null,
  regenerationVersion: null,
  contextEntity: null,
  navigationDrag: {entity:null,target:null,originTarget:null,suppressClickUntil:0,touch:null,sourceElement:null,placeholder:null,placeholderKey:null,ghost:null,dropping:false},
  confirmCallback: null,
  inputCallback: null,
  contractScroll: new Map(),
  contractOutline: {positions:[],frame:null,rebuild:false},
  contractSelections: new Map(),
  chat: {conversationId: null, messageCount: 0, eventSource: null, eventAfter: 0, liveStreamId: null, liveSource: '', liveCard: null, renderTimer: null, statusTimer: null, syncPromise: null, finalizedStreams: new Set(), syncedStreams: new Set(), busy: false},
  deletionJobs: new Map(),
  admin: {users: [], allUsers: [], groups: [], domains: [], ledger: []},
  adminMembership: {mode: null, targetId: null, selected: new Set(), query: ''},
  otpEmail: '',
  paperPreviewUrl: null,
  deploymentSloganTimer: null,
  websiteLanguage: 'zh',
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const uid = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
const UI_COPY = Object.freeze({
  zh: Object.freeze({
    institution:'单位（可选）',department:'院系（可选）',position:'身份（可选）',creditBalance:'Credit 余额',consumedCredits:'实际消耗 Credit',creditWindow1d:'1 天',creditWindow7d:'7 天',creditWindow30d:'30 天',creditWindowAll:'历史所有',collapseSidebar:'收起侧栏',expandSidebar:'展开侧栏',
    newResearch:'＋ 新建研究',research:'Research',unfiled:'未归档',sourceFiles:'Source Files',help:'使用说明',
    landingEyebrow:'From question to research',landingTitle:'提出一个问题，让研究从这里展开。',landingCopy:'写下你想研究的问题。MathLab 会整理研究边界、规划初始方向，并生成可执行的研究方案。',landingStart:'开始一个研究问题',landingStartFirst:'开始第一个研究问题',recentResearch:'最近研究',recentCopy:'最近更新的研究问题',workflow:'MathLab 如何工作',
    formulate:'把问题、假设和研究边界写清楚。',explore:'多条路线并行推进，并依据新结果动态调整。',consolidate:'汇总已证结论、难点与下一阶段研究任务。',
    backToRun:'返回 Research Run',contractSources:'Source Materials',discardDraft:'放弃草稿',saveVersion:'保存为新版本',cloneContract:'从此 Contract 新开对话',fullRegenerate:'完整重新生成新版本',chatSubtitle:'精简讨论与 Working Draft 助手',chatPlaceholder:'询问 Contract，或明确要求修改某个 A–F Section……',chatHint:'Enter 换行；Ctrl/Cmd + Enter 发送。明确要求修改时才更新 Working Draft。',send:'发送',
    contractMap:'Contract 结构总览',contractMapCopy:'从研究问题到执行设置；点击节点可直接跳转。',researchLanguageCopy:'本次 Run 创建后将冻结',
    viewContract:'查看锁定 Contract',pause:'暂停运行',resume:'继续运行',drain:'直接收束当前研究',events:'Streaming Events',eventsSubtitle:'局部判断与全局研究复盘',verified:'Verified Mathematics',verifiedSubtitle:'fresh cold-start verification',
    quickStart:'最快使用路径',helpOne:'填写并提交 Research Request',helpOneCopy:'最少只需把问题描述清楚并提交，等待 Contract 返回。',helpTwo:'修改并提交 Contract',helpTwoCopy:'对返回的 Contract 可直接双击分块编辑。编辑完成后即可提交研究。',helpThree:'等待研究并下载结果',helpThreeCopy:'研究约持续数小时，无需持续操作。',betaFeedback:'MathLab 目前处于开发阶段，请在项目仓库提交 issue。',
    displayName:'显示名称',websiteLanguage:'Website Language',defaultContractLanguage:'默认 Contract Language',defaultResearchLanguage:'默认 Research Language',defaultModel:'默认模型策略',defaultBudget:'默认预算预设',savePreferences:'保存 Preferences',signOut:'Sign out',contractLanguage:'Contract Language',requestTitle:'你真正想研究什么？',requestProblem:'1. 想研究的问题 <em>必填</em>',requestBackground:'2. 已知背景 / 已有进展 / 失败路线',requestMethods:'3. 方法偏好与限制',requestPartial:'4. 完整目标做不出时仍值得保留的结果',requestIdeas:'5. 可能的想法或大致做法',requestProject:'归档到 Project',requestProfile:'Contractor 档位',dropTitle:'拖动资料到这里，或点击选择文件',dropCopy:'支持 PDF、Markdown、TXT、TeX；单个文件不超过 50 MiB。',fromLibrary:'从资料库添加',cancel:'取消',saveDraft:'暂时保存',generateContract:'生成 Research Contract',unsaved:'尚未保存到服务端',costFirst:'成本优先',balanced:'平衡',capabilityFirst:'能力优先',selectUploaded:'选择已上传的资料…',noOtherFiles:'没有其他可用资料',deploymentCopy:'配置研究力量与时间窗口',chooseModel:'选择研究模型',chooseDuration:'选择研究时间窗口',qwenNote:'Qwen 经测试输出较慢，效果接近 GPT Low，建议仅在 GPT 不可用时作为备选方案。',lockContract:'锁定 Contract，开始研究',lockCopy:'执行后 Contract 的目标与 Source Materials 将锁定。',download:'下载',rename:'重命名',delete:'删除',remove:'移除',noFiles:'尚无文件。',saveSelection:'保存选择',
    noContract:'暂无 Contract',noProject:'尚无 Project',noUnfiled:'暂无未归档对话',waitingProgress:'等待下一步研究操作。',justNow:'刚刚',minutesAgo:'{n} 分钟前',hoursAgo:'{n} 小时前',daysAgo:'{n} 天前',
    waiting:'等待研究',researching:'研究中',draining:'正在收束',paused:'已暂停',resuming:'正在恢复',writing:'论文写作中',auditing:'论文审稿中',revising:'论文修订中',publishing:'正在发布',completed:'已完成',failed:'失败',interrupted:'已中断',draft:'草稿',planning:'规划中',planningInterrupted:'规划中断',planningFailed:'规划失败',ready:'待执行',
    runWaiting:'等待研究启动',runResearch:'正在研究',runDraining:'正在收束当前研究',runPaused:'研究已暂停',runResuming:'正在恢复研究',runWriting:'正在根据当前结果撰写报告',runAudit:'正在进行独立数学审稿',runRevision:'正在修订最终论文',runPublishing:'最终论文正在发布',runCompleted:'研究与论文已完成',runFailed:'研究运行失败',runInterrupted:'研究运行已中断',
    noEvents:'等待局部研究组或全局 Reviewer 的下一次判断。',noClaims:'尚无 fresh-verified Claim。',newResult:'新结果通过独立核验',proof:'查看证明过程',backStatement:'返回完整命题',proofUnavailable:'证明过程不可用',loadingProof:'正在载入证明…',
  }),
  en: Object.freeze({
    institution:'Institution (optional)',department:'Department (optional)',position:'Position (optional)',creditBalance:'Credit balance',consumedCredits:'Consumed credits',creditWindow1d:'1 day',creditWindow7d:'7 days',creditWindow30d:'30 days',creditWindowAll:'All time',collapseSidebar:'Collapse sidebar',expandSidebar:'Expand sidebar',
    newResearch:'+ New Research',research:'Research',unfiled:'Unfiled',sourceFiles:'Source Files',help:'Guide',
    landingEyebrow:'From question to research',landingTitle:'Ask a question and let the research unfold.',landingCopy:'Describe the problem you want to study. MathLab will clarify its boundaries, plan initial directions, and prepare an executable research program.',landingStart:'Start a research question',landingStartFirst:'Start your first research question',recentResearch:'Recent Research',recentCopy:'Recently updated research questions',workflow:'How MathLab works',
    formulate:'Clarify the problem, assumptions, and research boundary.',explore:'Advance several routes in parallel and adapt to new evidence.',consolidate:'Consolidate proved results, obstacles, and next research steps.',
    backToRun:'Back to Research Run',contractSources:'Source Materials',discardDraft:'Discard Draft',saveVersion:'Save New Version',cloneContract:'Branch from Contract',fullRegenerate:'Regenerate Full Version',chatSubtitle:'Focused discussion and Working Draft assistant',chatPlaceholder:'Ask about the Contract, or explicitly request a change to an A–F Section…',chatHint:'Enter inserts a line break; Ctrl/Cmd + Enter sends. The Working Draft changes only on an explicit request.',send:'Send',
    contractMap:'Contract Research Map',contractMapCopy:'From the research problem to deployment; select a node to jump to that Section.',researchLanguageCopy:'Frozen when this Run is created',
    viewContract:'View Locked Contract',pause:'Pause Run',resume:'Resume Run',drain:'Conclude Research',events:'Streaming Events',eventsSubtitle:'Local judgments and global research reviews',verified:'Verified Mathematics',verifiedSubtitle:'fresh cold-start verification',
    quickStart:'Quick Start',helpOne:'Submit a Research Request',helpOneCopy:'A clear problem statement is enough to begin. Submit it and wait for the Contract.',helpTwo:'Review and submit the Contract',helpTwoCopy:'Double-click a Contract block to edit it, then submit the finished Contract for research.',helpThree:'Wait for research and download the result',helpThreeCopy:'Research may run for several hours without intervention. A downloadable paper is produced at the end.',betaFeedback:'MathLab is currently in development. Please open an issue in the project repository.',
    displayName:'Display Name',websiteLanguage:'Website Language',defaultContractLanguage:'Default Contract Language',defaultResearchLanguage:'Default Research Language',defaultModel:'Default Model Strategy',defaultBudget:'Default Duration',savePreferences:'Save Preferences',signOut:'Sign out',contractLanguage:'Contract Language',requestTitle:'What would you like to research?',requestProblem:'1. Research problem <em>required</em>',requestBackground:'2. Background, prior progress, and failed routes',requestMethods:'3. Method preferences and restrictions',requestPartial:'4. Valuable outcomes if the full goal remains open',requestIdeas:'5. Possible ideas or rough approaches',requestProject:'Project',requestProfile:'Contractor Tier',dropTitle:'Drop source materials here, or select files',dropCopy:'PDF, Markdown, TXT, and TeX; up to 50 MiB per file.',fromLibrary:'Add from your library',cancel:'Cancel',saveDraft:'Save Draft',generateContract:'Generate Research Contract',unsaved:'Not saved to the server',costFirst:'Cost first',balanced:'Balanced',capabilityFirst:'Capability first',selectUploaded:'Select an uploaded file…',noOtherFiles:'No other files available',deploymentCopy:'Configure research capability, duration, and language',chooseModel:'Choose a research model',chooseDuration:'Choose a research window',qwenNote:'Qwen is slower in current testing and performs near GPT Low; use it mainly as a fallback when GPT is unavailable.',lockContract:'Lock Contract and Start Research',lockCopy:'The Contract goal and Source Materials are frozen after launch.',download:'Download',rename:'Rename',delete:'Delete',remove:'Remove',noFiles:'No files yet.',saveSelection:'Save Selection',
    noContract:'No Contracts',noProject:'No Projects',noUnfiled:'No unfiled conversations',waitingProgress:'Waiting for the next research action.',justNow:'just now',minutesAgo:'{n} min ago',hoursAgo:'{n} hr ago',daysAgo:'{n} days ago',
    waiting:'Waiting',researching:'Researching',draining:'Concluding',paused:'Paused',resuming:'Resuming',writing:'Writing paper',auditing:'Auditing paper',revising:'Revising paper',publishing:'Publishing',completed:'Completed',failed:'Failed',interrupted:'Interrupted',draft:'Draft',planning:'Planning',planningInterrupted:'Planning interrupted',planningFailed:'Planning failed',ready:'Ready',
    runWaiting:'Waiting to start',runResearch:'Research in progress',runDraining:'Concluding current research',runPaused:'Research paused',runResuming:'Resuming research',runWriting:'Writing the paper from current results',runAudit:'Independent mathematical audit in progress',runRevision:'Revising the final paper',runPublishing:'Publishing the final paper',runCompleted:'Research and paper completed',runFailed:'Research Run failed',runInterrupted:'Research Run interrupted',
    noEvents:'Waiting for the next local judgment or global review.',noClaims:'No fresh-verified Claims yet.',newResult:'New result independently verified',proof:'View proof',backStatement:'Back to statement',proofUnavailable:'Proof unavailable',loadingProof:'Loading proof…',
  }),
});
function uiLanguage(){return state.websiteLanguage==='en'?'en':'zh';}
function t(key,values={}){let text=UI_COPY[uiLanguage()][key]??UI_COPY.zh[key]??key;for(const [name,value] of Object.entries(values))text=text.replaceAll(`{${name}}`,String(value));return text;}
function applyWebsiteLanguage(language){
  state.websiteLanguage=language==='en'?'en':'zh';document.documentElement.lang=state.websiteLanguage==='en'?'en':'zh-CN';
  for(const field of ['institution','department','position']) $(`#pref-${field}-label`).textContent=t(field);
  for(const [id,key] of [['sidebar-collapse','collapseSidebar'],['sidebar-expand','expandSidebar']]){
    const button=$(`#${id}`);button.title=t(key);button.setAttribute('aria-label',t(key));
  }
  if(state.admin.users.length)renderAdminUsers();
  const bindings={
    '#new-contract':'newResearch','#sidebar-research-label':'research','#sidebar-unfiled-label':'unfiled','#files-button':'sourceFiles','#help-button':'help','#landing-eyebrow':'landingEyebrow','#landing-title':'landingTitle','#landing-copy':'landingCopy','#recent-research-title':'recentResearch','#recent-research-copy':'recentCopy','#workflow-title':'workflow',
    '.principle-grid article:nth-child(1) p':'formulate','.principle-grid article:nth-child(2) p':'explore','.principle-grid article:nth-child(3) p':'consolidate','#back-to-run':'backToRun','#contract-sources':'contractSources','#discard-working-draft':'discardDraft','#save-contract-version':'saveVersion','#clone-contract':'cloneContract','#contract-map-title':'contractMap','#contract-map-copy':'contractMapCopy','#deployment-research-language-copy':'researchLanguageCopy',
    '.chat-panel-title small':'chatSubtitle','#full-regenerate':'fullRegenerate','#contract-chat-form button[type="submit"]':'send','#view-locked-contract':'viewContract','#pause-resume-run':'pause','#graceful-drain-run':'drain','#run-view .compact-run-card:nth-child(2) .panel-title span':'events','#run-view .compact-run-card:nth-child(2) .panel-title small':'eventsSubtitle','#run-view .compact-run-card:nth-child(3) .panel-title span':'verified','#run-view .compact-run-card:nth-child(3) .panel-title small':'verifiedSubtitle',
    '#help-dialog h2':'quickStart','#help-dialog .workflow-list li:nth-child(1) strong':'helpOne','#help-dialog .workflow-list li:nth-child(1) span':'helpOneCopy','#help-dialog .workflow-list li:nth-child(2) strong':'helpTwo','#help-dialog .workflow-list li:nth-child(2) span':'helpTwoCopy','#help-dialog .workflow-list li:nth-child(3) strong':'helpThree','#help-dialog .workflow-list li:nth-child(3) span':'helpThreeCopy',
    '#pref-name-label':'displayName','#pref-website-language-label':'websiteLanguage','#pref-contract-language-label':'defaultContractLanguage','#pref-research-language-label':'defaultResearchLanguage','#pref-model-label':'defaultModel','#pref-budget-label':'defaultBudget','#save-preferences':'savePreferences','#logout-button':'signOut','#request-contract-language-label':'contractLanguage','#request-project-label':'requestProject','#request-profile-label':'requestProfile','#request-drop-title':'dropTitle','#request-drop-copy':'dropCopy','#request-library-label':'fromLibrary','#cancel-new-contract':'cancel','#save-request-draft':'saveDraft','#create-contract-submit':'generateContract','#deployment-title-copy':'deploymentCopy','#deployment-model-copy':'chooseModel','#deployment-duration-copy':'chooseDuration','#deployment-qwen-note':'qwenNote','#approve-contract-label':'lockContract','#library-file-dropzone > strong':'dropTitle','#library-file-dropzone > span':'dropCopy','#save-conversation-sources':'saveSelection',
  };
  for(const [selector,key] of Object.entries(bindings)){const element=$(selector);if(element)element.textContent=t(key);}
  $('#contract-chat-input').placeholder=t('chatPlaceholder');const hint=$('#contract-chat-form .chat-form-actions .support-note');if(hint)hint.textContent=t('chatHint');const feedback=$('#beta-feedback');if(feedback)feedback.innerHTML=t('betaFeedback');
  const requestLabels={problem:'requestProblem',background:'requestBackground',methods:'requestMethods',partial:'requestPartial',ideas:'requestIdeas'};for(const [block,key] of Object.entries(requestLabels)){const label=$(`[data-request-block="${block}"] > span`);if(label)label.innerHTML=t(key);}
  const profile=$('#request-profile');if(profile){profile.options[0].textContent=t('costFirst');profile.options[1].textContent=t('balanced');profile.options[2].textContent=t('capabilityFirst');}
  const requestPlaceholders=state.websiteLanguage==='en'?{problem:'For example: given a sequence a_n, determine whether its limit exists and how parameters affect it.',background:'Known lemmas, prior progress, failed routes, or the most relevant attached paper.',methods:'Preferred methods and assumptions that must not be strengthened.',partial:'Results still worth preserving if the full objective remains open.',ideas:'Unverified ideas or rough approaches that the research team should examine.'}:{problem:'例如：给定序列 a_n，研究其极限是否存在；若存在求精确极限，并说明参数变化时是否仍成立。',background:'例如：我已经证明了有界性；尝试过能量法但卡在某一余项；附件中有一篇最相关论文。',methods:'例如：优先考虑能量法和紧性，不希望增加额外正则性假设；如有必要可以考虑反例路线。',partial:'例如：若不能证明极限存在，证明 liminf / limsup 的非平凡范围，或证明“若极限存在则只能取某个值”，也值得保存。',ideas:'例如：怀疑能量法需要一个统一 coercivity estimate；也可以尝试构造临界参数附近的反例。这里的想法可以尚未验证。'};for(const [key,placeholder] of Object.entries(requestPlaceholders))$(`#request-${key}`).placeholder=placeholder;
  if(!state.request.conversationId)$('#request-dialog-title').textContent=t('requestTitle');
}
const REQUEST_FIELDS = {
  problem: {id: 'request-problem', heading: '1. 想研究的问题'},
  background: {id: 'request-background', heading: '2. 已知背景 / 已有进展 / 失败路线'},
  methods: {id: 'request-methods', heading: '3. 方法偏好与限制'},
  partial: {id: 'request-partial', heading: '4. 完整目标做不出时仍值得保留的结果'},
  ideas: {id: 'request-ideas', heading: '5. 可能的想法或大致做法'},
};
const CONTRACT_MAJOR_LABELS = {
  A: 'A · Mathematical Contract',
  B: 'B · Research Policy',
  C: 'C · Structural Diagnosis',
  D: 'D · Immediate Research Nodes',
  E: 'E · Structured Research Directions',
  F: 'F · Further Directions',
  G: 'G · Execution',
};
const CONTRACT_MAP_DESCRIPTIONS = Object.freeze({
  zh:Object.freeze({A:'问题与成功标准',B:'研究边界与策略',C:'结构与关键障碍',D:'立即执行的节点',E:'条件成熟的方向',F:'更远研究路线',G:'模型、时间与语言'}),
  en:Object.freeze({A:'Problem and success criteria',B:'Research boundary and policy',C:'Structure and key obstacles',D:'Immediate research nodes',E:'Structured next directions',F:'Longer-range directions',G:'Model, duration, and language'}),
});
const EXECUTION_MODEL_TIER_ALIASES = Object.freeze({
  balanced: 'gpt_medium',
  capability: 'gpt_medium',
  capability_priority: 'gpt_medium',
  deepseek_only: 'qwen_only',
  gpt_deepseek_mixture: 'gpt_medium',
  gpt_qwen_mixture: 'gpt_medium',
});
const SELECTABLE_EXECUTION_MODEL_TIERS = Object.freeze(['gpt_low','gpt_medium','gpt_high','qwen_only']);
const SELECTABLE_EXECUTION_DURATION_TIERS = Object.freeze(['quick_1h','balanced_3h','detailed_6h']);
const EXECUTION_MODEL_LABELS = Object.freeze({gpt_low:'GPT Low',gpt_medium:'GPT Medium',gpt_high:'GPT High',qwen_only:'Qwen'});
const EXECUTION_DURATION_LABELS = Object.freeze({quick_1h:'1h',balanced_3h:'3h',detailed_6h:'6h'});
const DEPLOYMENT_MODEL_META = Object.freeze({
  gpt_low: Object.freeze({subtitleLines:Object.freeze(['Balanced','Cost-effective']),icon:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M19 5l-2 2M7 17l-2 2"/></svg>'}),
  gpt_medium: Object.freeze({subtitleLines:Object.freeze(['Balanced','Deep Research']),icon:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 7 7-7 7-7-7 7-7Zm-4 12-3 3 7 3 7-7-3-3"/></svg>'}),
  gpt_high: Object.freeze({subtitleLines:Object.freeze(['High Quality','High Intensity']),icon:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2c1.1 5.5 4.5 8.9 10 10-5.5 1.1-8.9 4.5-10 10-1.1-5.5-4.5-8.9-10-10 5.5-1.1 8.9-4.5 10-10Z"/></svg>'}),
  qwen_only: Object.freeze({subtitleLines:Object.freeze(['Reserve','Backup']),icon:'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2"/><path d="M12 3c4 2 6 5 6 9s-2 7-6 9"/></svg>'}),
});
const DEPLOYMENT_CHECK_ICON = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';
const EXECUTION_SLOGANS = Object.freeze({
  gpt_low: Object.freeze({
    quick_1h:'给我一组靠谱博士生牛马，记得一人配一杯瑞幸九块九！',
    balanced_3h:'即使伟大的科学有赖运气，也不妨站到最容易迎来闪电的山顶。',
    detailed_6h:'我们或许不会马上成功，但将屡败屡战，百战不挠。',
  }),
  gpt_medium: Object.freeze({
    quick_1h:'机器经常让我大吃一惊。',
    balanced_3h:'所有稳定的过程，我们将预测；\n所有不稳定的过程，我们将控制。',
    detailed_6h:'我们选择登月，不是因为它容易，而是因为它困难。',
  }),
  gpt_high: Object.freeze({
    quick_1h:'春宵一刻值千金。',
    balanced_3h:'我们必须知道，我们必将知道。',
    detailed_6h:'你的花费无人知晓，你的功绩永世长存。',
  }),
  qwen_only: Object.freeze({
    quick_1h:'原计划里没有这一段。',
    balanced_3h:'我未必想做那个挺身而出的人，但总得有挺身而出的本事。',
    detailed_6h:'我不是第一个被想起的人，但可以是最后一个离开的人。',
  }),
});
const EXECUTION_SLOGAN_ATTRIBUTIONS = Object.freeze({
  gpt_low: Object.freeze({
    quick_1h:null,
    balanced_3h:'— Richard Hamming',
    detailed_6h:'— Franklin D. Roosevelt, 1944',
  }),
  gpt_medium: Object.freeze({
    quick_1h:'— Alan Turing, 1950',
    balanced_3h:'— John von Neumann',
    detailed_6h:'— John F. Kennedy, 1962',
  }),
  gpt_high: Object.freeze({
    quick_1h:'— 苏轼',
    balanced_3h:'— David Hilbert',
    detailed_6h:'— 改写自无名烈士纪念碑碑文',
  }),
  qwen_only: Object.freeze({quick_1h:null,balanced_3h:null,detailed_6h:null}),
});
// Verified sources and editorial notes: docs/ENGLISH_DEPLOYMENT_QUOTES.md.
const EXECUTION_QUOTES_EN = Object.freeze({
  gpt_low: Object.freeze({
    quick_1h:{text:'Many hands make light work.',attribution:'— English proverb'},
    balanced_3h:{text:'You can stand on a mountain top where lightning strikes.',attribution:'— Richard Hamming, 1986'},
    detailed_6h:{text:'We shall strive for perfection. We shall not achieve it immediately—but we still shall strive.',attribution:'— Franklin D. Roosevelt, 1945'},
  }),
  gpt_medium: Object.freeze({
    quick_1h:{text:'Machines take me by surprise with great frequency.',attribution:'— Alan Turing, 1950'},
    balanced_3h:{text:'Stable regions we can predict.\nUnstable regions we can control.',attribution:'— John von Neumann, recalled by Freeman Dyson'},
    detailed_6h:{text:'We choose to go to the moon … not because they are easy, but because they are hard.',attribution:'— John F. Kennedy, 1962'},
  }),
  gpt_high: Object.freeze({
    quick_1h:{text:'Make use of time, let not advantage slip.',attribution:'— William Shakespeare, Venus and Adonis'},
    balanced_3h:{text:'We must know. We will know.',attribution:'— David Hilbert, 1930 (English translation)'},
    detailed_6h:{text:'The growing good of the world is partly dependent on unhistoric acts.',attribution:'— George Eliot, Middlemarch'},
  }),
  qwen_only: Object.freeze({
    quick_1h:{text:'The readiness is all.',attribution:'— William Shakespeare, Hamlet'},
    balanced_3h:{text:'They also serve who only stand and wait.',attribution:'— John Milton, Sonnet 19'},
    detailed_6h:{text:'To strive, to seek, to find, and not to yield.',attribution:'— Alfred, Lord Tennyson, Ulysses'},
  }),
});
const DEPLOYMENT_BACKGROUNDS = Object.freeze({
  gpt_low: Object.freeze({
    quick_1h:'/static/assets/deployment/gpt-low-1h.webp',
    balanced_3h:'/static/assets/deployment/gpt-low-3h.webp',
    detailed_6h:'/static/assets/deployment/gpt-low-6h.webp',
  }),
  gpt_medium: Object.freeze({
    quick_1h:'/static/assets/deployment/gpt-medium-1h.webp',
    balanced_3h:'/static/assets/deployment/gpt-medium-3h.webp',
    detailed_6h:'/static/assets/deployment/gpt-medium-6h.webp',
  }),
  gpt_high: Object.freeze({
    quick_1h:'/static/assets/deployment/gpt-high-1h.webp',
    balanced_3h:'/static/assets/deployment/gpt-high-3h.webp',
    detailed_6h:'/static/assets/deployment/gpt-high-6h.webp',
  }),
  qwen_only: Object.freeze({
    quick_1h:'/static/assets/deployment/qwen-1h.webp',
    balanced_3h:'/static/assets/deployment/qwen-3h.webp',
    detailed_6h:'/static/assets/deployment/qwen-6h.webp',
  }),
});
const RESEARCH_LOADING_QUOTES = Object.freeze([
  '今晚又有 AI 要睡不着觉了，哈哈！',
  '我还没有失败。我只是又排除了一种错误答案。',
  '组会还有十二个小时，一切都还来得及。',
  '今晚没有人催我们，除了明早的组会。',
  '办公室的灯还亮着。看来事情还有希望。',
  '学会解决每一个已经被解决的问题。',
  '我们只能看到前方很短的一段路，但那里已经有很多事等着去做。',
  '一个人熬夜叫失眠，一群人熬夜叫科研。',
  '夜还很长，问题也是。',
  '行到水穷处，坐看云起时。',
  '路漫漫其修远兮，吾将上下而求索。',
  '山重水复疑无路，柳暗花明又一村。',
  '坏消息：这条路不通。好消息：我们现在知道了。',
  '答案不欠我们一个简单的证明。',
  '烧掉的是 Token，留下的是历史。',
  '故事未必要很长，才值得记得很久。',
  '有些答案与爱情一样，强求未必有结果。',
  '事情已经发展到了需要有AI站出来的地步。',
  '不知道，并不是一个坏的起点。',
  '复杂并不可怕。混乱才是。',
]);
const RESEARCH_LOADING_QUOTES_EN = Object.freeze([
  'A hard problem is still a useful place to begin.',
  'Several routes are being tested in parallel.',
  'A failed route can still sharpen the theorem.',
  'The answer does not owe us a short proof.',
  'Complexity is manageable; confusion is the real enemy.',
]);
function normalizedExecutionModelTier(value) {
  const tier=String(value||'').trim();
  return EXECUTION_MODEL_TIER_ALIASES[tier]||tier||'gpt_medium';
}
function normalizedExecutionDurationTier(value) {
  const tier=String(value||'').trim();
  return tier==='large'?'balanced_3h':(tier||'balanced_3h');
}

function freshChatState(conversationId=null, contractVersionId=null, selectedVersion=null, messageCount=0, eventAfter=0) {
  return {conversationId,contractVersionId,selectedVersion,messageCount,eventSource:null,eventAfter,liveStreamId:null,liveSource:'',liveCard:null,renderTimer:null,statusTimer:null,syncPromise:null,finalizedStreams:new Set(),syncedStreams:new Set(),busy:false};
}

function freshRunUiState() {
  return {eventSequences:new Set(),eventUnread:0,claimIds:new Set(),claimUnread:0,claimsInitialized:false,claimProofs:new Map(),activeClaimId:null,verifiedClaimView:'statement',graphFingerprint:'',graphInteractionVersion:0,loadingQuoteTimer:null,loadingQuoteFadeTimer:null,loadingQuoteIndex:-1};
}

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const part = document.cookie.split('; ').find(item => item.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : '';
}

async function api(path, options = {}, {allowUnauthorized = false} = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const method = String(options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = cookieValue('mathlab_csrf');
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }
  const response = await fetch(path, {...options, headers, credentials: 'same-origin',cache:options.cache||(method==='GET'?'no-store':'default')});
  if (response.status === 401 && !allowUnauthorized) showLogin();
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response.text();
}

async function downloadAuthenticated(path, filename) {
  const response = await fetch(path, {credentials: 'same-origin'});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `${response.status}`);
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

async function renderMarkdown(container, source) {
  if (window.MathLabRender?.renderMarkdown) return MathLabRender.renderMarkdown(container, source || '');
  container.textContent = source || '';
}
async function renderMarkdownBatch(entries) {
  if (window.MathLabRender?.renderMarkdownBatch) return MathLabRender.renderMarkdownBatch(entries);
  for (const entry of entries) await renderMarkdown(entry.container, entry.source);
}
async function renderMathText(container, source) {
  if (window.MathLabRender?.renderMathText) return MathLabRender.renderMathText(container, source || '');
  container.textContent = source || '';
}
async function renderMathTextBatch(entries) {
  if (window.MathLabRender?.renderMathTextBatch) return MathLabRender.renderMathTextBatch(entries);
  for (const entry of entries) await renderMathText(entry.container, entry.source);
}
async function renderMathTextIfChanged(container, source) {
  const normalized=String(source||'');
  if(!container||container.dataset.mathSource===normalized)return;
  await renderMathText(container,normalized);
  container.dataset.mathSource=normalized;
}
function number(value) { return Number(value || 0).toLocaleString(); }
function money(value) { return `${Number(value || 0).toFixed(2)} Credits`; }
function compactCredit(value) { const n=Number(value||0); return `${Number.isInteger(n)?n:n.toFixed(2).replace(/0+$/,'').replace(/\.$/,'')} Credits`; }
function tokenMillions(value) { return `${(Number(value||0)/1_000_000).toFixed(2)}M`; }
function formatBytes(bytes) { if (bytes < 1024) return `${bytes} B`; if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`; return `${(bytes / 1024 ** 2).toFixed(1)} MiB`; }

function toast(message, error = false) {
  const item = document.createElement('div');
  item.className = `toast${error ? ' error' : ''}`;
  const copy = document.createElement('span'); copy.textContent = message;
  const close = document.createElement('button'); close.type = 'button'; close.setAttribute('aria-label', '关闭'); close.textContent = '×'; close.onclick = () => item.remove();
  item.append(copy, close);
  $('#toast-region').append(item);
  setTimeout(() => item.remove(), 5000);
}
function openDialog(selector) { const dialog = $(selector); if (dialog && !dialog.open) dialog.showModal(); }
function closeDialog(selector) { const dialog = $(selector); if (dialog?.open) dialog.close(); }
function showLogin() { window.location.replace('/login'); }
function showAlert(title, message) { $('#alert-title').textContent = title; $('#alert-message').textContent = message; openDialog('#alert-dialog'); }
function showView(name) { if(name!=='run')stopRunLoadingQuotes();state.activeView=name; for (const id of ['landing-view','conversation-view','run-view']) $(`#${id}`).classList.add('hidden'); $(`#${name}-view`).classList.remove('hidden'); $('#page-subtitle').classList.toggle('hidden',name!=='landing'); }
function goHome(){rememberContractScroll();rememberContractSelection();closeContractChatStream();stopGenerationElapsedTimer();stopRunPolling();state.conversation=null;state.run=null;state.viewingRunContract=false;state.selected={kind:'version',version:null};renderNavigation();renderLanding();showView('landing');renderMathText($('#breadcrumb'),'MathLab');renderMathText($('#page-title'),'Research Contracts');}
function setSaveState(selector, text, kind = '') { const el = $(selector); if (!el) return; el.textContent = text; el.className = `save-state${kind ? ` ${kind}` : ''}`; }
function renderRequestGenerationEstimate() {
  const profile=$('#request-profile')?.value||'high';
  const catalog=state.bootstrap?.contract_generation_catalog||{};
  const item=catalog[profile]||catalog.high;
  const target=$('#request-generation-estimate');
  if(!target||!item)return;
  const reserve=Number(item.precharge_credit||0);
  const minutes=Number(item.estimated_minutes||0);
  target.textContent=uiLanguage()==='en'?`Precharge ${Number.isInteger(reserve)?reserve:reserve.toFixed(2)} Credits · about ${minutes} min`:`预扣 ${Number.isInteger(reserve)?reserve:reserve.toFixed(2)} Credit · 预计约 ${minutes} 分钟`;
}

function confirmAction(title, message, onAccept, acceptLabel = '确认') {
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-accept').textContent = acceptLabel;
  state.confirmCallback = onAccept;
  openDialog('#confirm-dialog');
}

function inputAction({title, label, value = '', eyebrow = 'Input', acceptLabel = '确认'}, onAccept) {
  $('#input-title').textContent = title;
  $('#input-label').firstChild.textContent = label;
  $('#input-value').value = value;
  $('#input-eyebrow').textContent = eyebrow;
  $('#input-accept').textContent = acceptLabel;
  state.inputCallback = onAccept;
  openDialog('#input-dialog');
  requestAnimationFrame(() => { $('#input-value').focus({preventScroll:true}); $('#input-value').select(); });
}

async function withBusy(button, task) {
  if (!button || button.disabled) return;
  const label = button.textContent; button.disabled = true; button.dataset.busy = 'true'; button.textContent = '处理中…';
  try { return await task(); }
  finally { button.disabled = false; delete button.dataset.busy; button.textContent = label; if(button.matches('#save-request-draft,#create-contract-submit'))updateRequestUploadState(); }
}

function clearIdentityScopedDom() {
  clearTimeout(state.request.autosaveTimer);
  clearTimeout(state.requestEditor.previewTimer);
  clearTimeout(state.section.previewTimer);
  clearTimeout(state.section.saveTimer);
  stopGenerationPolling(); stopGenerationElapsedTimer(); stopRunPolling(); stopRunLoadingQuotes(); stopResearchOverviewPolling(); closeContractChatStream();
  if(state.contractOutline.frame)cancelAnimationFrame(state.contractOutline.frame);
  state.contractOutline={positions:[],frame:null,rebuild:false};
  state.conversation = null; state.run = null; state.viewingRunContract = false;
  state.runUi = freshRunUiState();
  state.request = {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set(), lastSavedFingerprint: null, pendingUploads: 0};
  state.fileLibrary = {pendingUploads: 0};
  state.requestEditor = {key: null, previewTimer: null, syncing: false};
  state.contextEntity = null;
  state.researchOverviewFingerprint='';
  state.contractScroll.clear(); state.contractSelections.clear(); state.chat = freshChatState(); for(const timer of state.deletionJobs.values())clearInterval(timer); state.deletionJobs.clear();
  for (const id of ['request-problem','request-background','request-methods','request-partial','request-ideas','request-editor-source','contract-chat-input','section-editor']) {
    const el = $(`#${id}`); if (el) el.value = '';
  }
  closeEntityContextMenu();
  closeDialog('#request-editor-dialog');
  $('#contract-chat-list').innerHTML = '';
  $('#contract-sections').innerHTML = '';
}

async function refreshBootstrap({keepConversation = true} = {}) {
  try {
    const data = await api('/api/bootstrap', {}, {allowUnauthorized: true});
    const initialPhases=new Map((data.recent_research||[]).map(item=>[item.conversation_id,item.runtime_phase]));
    for(const item of data.conversations||[])if(!item.runtime_phase&&initialPhases.get(item.conversation_id))item.runtime_phase=initialPhases.get(item.conversation_id);
    state.bootstrap = data;
    applyWebsiteLanguage(data.current_user?.website_language||'zh');
    closeDialog('#login-dialog');
    renderUser(data.current_user, data.usage);
    renderNavigation();
    renderLanding();
    for(const item of [...(data.projects||[]),...(data.conversations||[])])if(item.deletion_job)trackDeletion(item.deletion_job.deletion_job_id);
    const generating=(data.conversations||[]).some(item=>item.status==='contract_generating');
    $('#generation-notice').classList.toggle('hidden',!generating||state.generationNoticeClosed);
    renderFileLibrary();
    fillProjectSelects();
    if (!keepConversation || !state.conversation) {
      showView('landing');
      renderMathText($('#breadcrumb'), 'MathLab');
      renderMathText($('#page-title'), 'Research Contracts');
    }
    startGenerationPollingIfNeeded();
    startResearchOverviewPolling();
    return true;
  } catch (error) {
    if (error.status === 401) { showLogin(); return false; }
    throw error;
  }
}

function stopResearchOverviewPolling(){if(state.researchOverviewTimer){clearTimeout(state.researchOverviewTimer);state.researchOverviewTimer=null;}}
function startResearchOverviewPolling(){stopResearchOverviewPolling();const tick=async()=>{state.researchOverviewTimer=null;try{const data=await api('/api/research-overview');const fingerprint=JSON.stringify(data);if(fingerprint!==state.researchOverviewFingerprint){state.researchOverviewFingerprint=fingerprint;const byId=new Map((data.conversations||[]).map(item=>[item.conversation_id,item]));const phases=new Map((data.recent_research||[]).map(item=>[item.conversation_id,item.runtime_phase]));for(const item of state.bootstrap?.conversations||[]){const latest=byId.get(item.conversation_id);if(latest)Object.assign(item,latest);const recentPhase=phases.get(item.conversation_id);if(recentPhase&&(!latest||!latest.runtime_phase))item.runtime_phase=recentPhase;}if(state.bootstrap)state.bootstrap.recent_research=data.recent_research||[];renderNavigation();renderLanding();}}catch(error){console.warn(error);}finally{state.researchOverviewTimer=setTimeout(tick,2500);}};state.researchOverviewTimer=setTimeout(tick,2500);}

function renderUsagePanel(usage) {
  $('#usage-window').value=usage.window||'7d';
  const accountTiles = [
    ['Credit', money(usage.credit_balance)],
    ['Active runs', `${usage.active_runs}/${usage.max_concurrent_research_runs}`],
    ['Durable storage', `${formatBytes(usage.durable_storage_bytes||0)} / ${usage.storage_quota_bytes==null?'Unlimited':formatBytes(usage.storage_quota_bytes)}`],
  ];
  const usageTiles = [
    ['Model calls', number(usage.calls)],
    ['Total input', tokenMillions(usage.input_tokens)],
    ['Cached input', tokenMillions(usage.cached_input_tokens)],
    ['Total output', tokenMillions(usage.output_tokens)],
  ];
  $('#account-summary').innerHTML=accountTiles.map(([a,b])=>`<article><strong>${escapeHtml(b)}</strong><span>${escapeHtml(a)}</span></article>`).join('');
  $('#usage-grid').innerHTML=usageTiles.map(([a,b])=>`<article><strong>${escapeHtml(b)}</strong><span>${escapeHtml(a)}</span></article>`).join('');
}
async function refreshUsageWindow() {
  const windowKey=$('#usage-window').value||'7d';
  const usage=await api(`/api/users/me/usage?window=${encodeURIComponent(windowKey)}`);
  renderUsagePanel(usage);
}
function renderUser(user, usage) {
  $('#user-name').textContent = user.display_name;
  $('#user-role').textContent = `${user.role}${user.is_developer?' · developer':''} · ${(user.groups||[]).join(', ')||'No group'}`;
  $('#user-avatar').textContent = (user.display_name || 'U').slice(0, 1).toUpperCase();
  $('#user-dialog-name').textContent = user.display_name;
  $('#pref-name').value = user.display_name;
  for(const field of ['institution','department','position']) $(`#pref-${field}`).value=user[field]||'';
  $('#pref-model').value = normalizedExecutionModelTier(user.default_model_strategy);
  $('#pref-budget').value = normalizedExecutionDurationTier(user.default_budget_preset);
  $('#pref-website-language').value = user.website_language||'zh';
  $('#pref-contract-language').value = user.default_contract_language||'zh';
  $('#pref-research-language').value = user.default_research_language||'zh';
  document.documentElement.style.setProperty('--chat-width', `${Math.round((user.contract_chat_panel_width || .34) * 100)}%`);
  renderUsagePanel(usage);
  const switcher = $('#dev-user-switch');
  switcher.innerHTML = state.bootstrap.application.dev_mode ? `<strong>本地测试用户</strong>${state.bootstrap.dev_users.map(item => `<button class="dev-user-button" type="button" data-user-id="${item.user_id}"><span>${escapeHtml(item.display_name)}</span><small>${escapeHtml(item.email || item.user_id)}</small></button>`).join('')}` : '';
  $('#open-admin').classList.toggle('hidden', user.role !== 'admin');
}

function conversationMarker(item) {
  if (item.status === 'completed') return '✓';
  if (item.status === 'contract_generating') return '◐';
  if (item.status === 'request_draft') return '✎';
  if (['execution_locked','running'].includes(item.status)) return '◉';
  return '◌';
}
function conversationStatusLabel(item) {
  const phaseLabels={waiting:t('waiting'),research:t('researching'),draining:t('draining'),paused:t('paused'),resuming:t('resuming'),paper_writing:t('writing'),paper_audit:t('auditing'),paper_revision:t('revising'),paper_published:t('publishing'),completed:t('completed'),failed:t('failed'),interrupted:t('interrupted')};
  if(item.runtime_phase&&phaseLabels[item.runtime_phase])return phaseLabels[item.runtime_phase];
  const runLabels={created:t('waiting'),queued:t('waiting'),resuming:t('resuming'),running:t('researching'),draining:t('draining'),paused:t('paused'),completed:t('completed'),failed:t('failed'),interrupted:t('interrupted')};
  if(item.run_status&&runLabels[item.run_status])return runLabels[item.run_status];
  const labels={request_draft:t('draft'),contract_generating:t('planning'),generation_interrupted:t('planningInterrupted'),generation_failed:t('planningFailed'),ready:t('ready'),execution_locked:t('waiting'),running:t('researching'),completed:t('completed'),run_deleted:t('ready')};
  return labels[item.status]||t('ready');
}
function conversationMeta(item) {
  const label=conversationStatusLabel(item);
  return item.latest_contract_version>0?`${label} · v${item.latest_contract_version}`:label;
}
function createChatRow(item,mathEntries) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `entity-row chat-row navigation-draggable${state.conversation?.conversation_id === item.conversation_id ? ' active' : ''}${item.deletion_job ? ' pending-deletion' : ''}`;
  button.dataset.conversationId = item.conversation_id;
  button.dataset.projectId = item.project_id || '';
  button.draggable = false;
  button.title = item.deletion_job ? '' : '长按并拖动可重新排序或归档到 Project';
  const marker = document.createElement('span'); marker.textContent = conversationMarker(item);
  const title = document.createElement('span'); title.className = 'entity-title math-text'; title.textContent = item.display_name;
  const meta = document.createElement('small'); meta.textContent = item.deletion_job ? '正在终止并删除' : conversationMeta(item);
  button.append(marker, title, meta); mathEntries.push({container:title,source:item.display_name});
  button.addEventListener('contextmenu', event => {
    event.preventDefault(); event.stopPropagation();
    if (state.navigationDrag.touch?.active) return;
    openEntityContextMenu(event, {type:'conversation', id:item.conversation_id, name:item.display_name});
  });
  button.addEventListener('dblclick', event => { event.preventDefault(); event.stopPropagation(); openConversationManager(item.conversation_id); });
  button.addEventListener('pointerdown', event => beginNavigationLongPress(event, {type:'conversation',id:item.conversation_id}, button));
  return button;
}
function renderNavigation() {
  const mathEntries=[];
  const conversations = state.bootstrap.conversations || [];
  const byProject = new Map((state.bootstrap.projects || []).map(p => [p.project_id, []]));
  const unfiled = [];
  for (const c of conversations) (c.project_id && byProject.has(c.project_id) ? byProject.get(c.project_id) : unfiled).push(c);
  const projectList = $('#project-list'); projectList.replaceChildren();
  for (const project of state.bootstrap.projects || []) {
    const block = document.createElement('div'); block.className = 'project-block'; block.dataset.projectId = project.project_id;
    const head = document.createElement('button'); head.type = 'button'; head.className = `entity-row project-heading navigation-draggable${project.deletion_job ? ' pending-deletion' : ''}`; head.dataset.projectId = project.project_id;
    head.draggable = false;
    head.title = project.deletion_job ? '' : '长按并拖动可调整 Project 顺序';
    const arrow = document.createElement('span'); arrow.textContent = '▾';
    const title = document.createElement('span'); title.className = 'entity-title math-text'; title.textContent = project.name;
    const menu = document.createElement('span'); menu.className = 'project-actions'; menu.dataset.projectMenu = project.project_id; menu.textContent = project.deletion_job ? '正在终止并删除' : '•••';
    head.append(arrow,title,menu); block.append(head); mathEntries.push({container:title,source:project.name});
    head.addEventListener('contextmenu', event => {
      event.preventDefault(); event.stopPropagation();
      if (state.navigationDrag.touch?.active) return;
      openEntityContextMenu(event, {type:'project', id:project.project_id, name:project.name});
    });
    head.addEventListener('dblclick', event => { event.preventDefault(); event.stopPropagation(); openProjectManager(project.project_id); });
    head.addEventListener('pointerdown', event => beginNavigationLongPress(event, {type:'project',id:project.project_id}, head));
    const chats = document.createElement('div'); chats.className = 'project-chats';
    const rows = byProject.get(project.project_id) || [];
    if (rows.length) rows.forEach(c => chats.append(createChatRow(c,mathEntries))); else chats.innerHTML = `<div class="empty-nav">${t('noContract')}</div>`;
    block.append(chats); projectList.append(block);
  }
  if (!state.bootstrap.projects?.length) projectList.innerHTML = `<div class="empty-nav">${t('noProject')}</div>`;
  const unfiledList = $('#unfiled-list'); unfiledList.replaceChildren();
  if (unfiled.length) unfiled.forEach(c => unfiledList.append(createChatRow(c,mathEntries))); else unfiledList.innerHTML = `<div class="empty-nav">${t('noUnfiled')}</div>`;
  renderMathTextBatch(mathEntries);
}

function recentResearchStatus(item) {
  const phaseLabels={waiting:t('waiting'),research:t('researching'),draining:t('draining'),paused:t('paused'),resuming:t('resuming'),paper_writing:t('writing'),paper_audit:t('auditing'),paper_revision:t('revising'),paper_published:t('publishing'),completed:t('completed'),failed:t('failed'),interrupted:t('interrupted')};
  if(item.runtime_phase)return {label:phaseLabels[item.runtime_phase]||'研究中',tone:item.runtime_phase};
  const label=conversationStatusLabel({status:item.conversation_status});
  const tone=['generation_failed'].includes(item.conversation_status)?'failed':(['generation_interrupted'].includes(item.conversation_status)?'interrupted':'planning');
  return {label,tone};
}
function recentResearchFallback(item) {
  if(uiLanguage()==='en'){
    const phaseCopy={waiting:'The research task is waiting in the queue.',research:'Several research directions are in progress.',draining:'Current research is being consolidated.',paused:'Research is safely paused with its artifacts and position preserved.',resuming:'The saved Agent threads and research position are being restored.',paper_writing:'The paper is being written from the mathematical results.',paper_audit:'The paper is undergoing an independent mathematical audit.',paper_revision:'The Writer is revising the paper from the audit.',paper_published:'The final paper is being published.',completed:'Research is complete and the final result is available.',failed:'The Research Run failed; inspect the record before retrying.',interrupted:'Research was interrupted; saved records remain available.'};
    if(item.runtime_phase&&phaseCopy[item.runtime_phase])return phaseCopy[item.runtime_phase];
    const version=item.latest_contract_version||0;const copy={request_draft:'The Research Request draft is waiting for submission.',contract_generating:'MathLab is clarifying the research boundary and initial directions.',generation_interrupted:'Contract generation was interrupted and can be resumed.',generation_failed:'Contract generation failed and can be retried.',ready:version?`Contract v${version} is ready for review or execution.`:'The Research Contract is waiting to be generated.',execution_locked:'The Contract is locked and the Run is waiting to start.',run_deleted:'The original Run was deleted; a new branch can be created from the Contract.'};return copy[item.conversation_status]||t('waitingProgress');
  }
  const phaseCopy={waiting:'研究任务已进入等待队列。',research:'多个研究方向正在推进。',draining:'正在收束当前研究并冻结阶段结论。',paused:'研究已安全暂停，当前数学产物和运行位置均已保存。',resuming:'正在恢复暂停前的研究任务与 Agent threads。',paper_writing:'正在根据现有数学结果撰写论文。',paper_audit:'论文正在接受独立数学审稿。',paper_revision:'Writer 正在根据审稿意见修订论文。',paper_published:'最终论文正在发布。',completed:'研究已完成，最终结果可供查看。',failed:'研究运行失败，可查看记录后决定是否重试。',interrupted:'研究已中断，可查看已保留的研究记录。'};
  if(item.runtime_phase&&phaseCopy[item.runtime_phase])return phaseCopy[item.runtime_phase];
  const version=item.latest_contract_version||0;
  const copy={request_draft:'Research Request 草稿等待提交。',contract_generating:'正在整理研究边界与初始方向。',generation_interrupted:'Contract 生成已中断，可以继续执行。',generation_failed:'Contract 生成失败，可以查看错误并重试。',ready:version?`Contract v${version} 已生成，等待修改或执行。`:'Research Contract 等待生成。',execution_locked:'Contract 已锁定，研究任务等待启动。',run_deleted:'原 Research Run 已删除，可从 Contract 新开研究。'};
  return copy[item.conversation_status]||t('waitingProgress');
}
function recentTime(value) {
  const timestamp=Date.parse(value);if(!Number.isFinite(timestamp))return '';
  const seconds=Math.max(0,Math.floor((Date.now()-timestamp)/1000));
  if(seconds<60)return t('justNow');
  if(seconds<3600)return t('minutesAgo',{n:Math.floor(seconds/60)});
  if(seconds<86400)return t('hoursAgo',{n:Math.floor(seconds/3600)});
  if(seconds<604800)return t('daysAgo',{n:Math.floor(seconds/86400)});
  return new Intl.DateTimeFormat(uiLanguage()==='en'?'en-US':'zh-CN',{month:'numeric',day:'numeric'}).format(new Date(timestamp));
}
function renderLanding() {
  const mathEntries=[];
  const items=state.bootstrap?.recent_research||[];
  $('#landing-new-research').textContent=items.length?t('landingStart'):t('landingStartFirst');
  $('#recent-research').classList.toggle('hidden',!items.length);
  const list=$('#recent-research-list');list.replaceChildren();
  for(const item of items){
    const button=document.createElement('button');button.type='button';button.className='recent-research-item';button.dataset.conversationId=item.conversation_id;
    const title=document.createElement('span');title.className='recent-research-title math-text';title.textContent=item.display_name;
    const status=recentResearchStatus(item);const badge=document.createElement('span');badge.className=`recent-research-status ${status.tone}`;badge.textContent=status.label;
    const progress=document.createElement('span');progress.className='recent-research-progress math-text';progress.textContent=item.latest_progress?.summary||recentResearchFallback(item);
    const time=document.createElement('span');time.className='recent-research-time';time.textContent=recentTime(item.updated_at);
    button.append(title,badge,progress,time);list.append(button);mathEntries.push({container:title,source:item.display_name},{container:progress,source:item.latest_progress?.summary||recentResearchFallback(item)});
  }
  renderMathTextBatch(mathEntries);
}

function navigationLayoutItems() {
  return $$('#project-list .project-block,#project-list .chat-row,#unfiled-list .chat-row')
    .filter(element=>!element.classList.contains('navigation-source-detached'));
}
function navigationItemKey(element) {
  if (element.classList.contains('project-block')) return `project:${element.dataset.projectId}`;
  return `conversation:${element.dataset.conversationId}`;
}
function captureNavigationLayout() {
  return new Map(navigationLayoutItems().map(element => [navigationItemKey(element), element.getBoundingClientRect()]));
}
function animateNavigationLayoutFrom(before) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  for (const element of navigationLayoutItems()) {
    const previous = before.get(navigationItemKey(element));
    if (!previous) continue;
    const current = element.getBoundingClientRect();
    const x = previous.left - current.left;
    const y = previous.top - current.top;
    if (Math.abs(x) < .5 && Math.abs(y) < .5) continue;
    element.getAnimations().forEach(animation => animation.cancel());
    element.animate(
      [{transform:`translate(${x}px,${y}px)`},{transform:'translate(0,0)'}],
      {duration:190,easing:'cubic-bezier(.2,.8,.2,1)'},
    );
  }
}
function mutateNavigationLayout(mutator) {
  const before = captureNavigationLayout();
  mutator();
  animateNavigationLayoutFrom(before);
}
function navigationSourceElement(entity) {
  return entity.type === 'project'
    ? $(`#project-list .project-block[data-project-id="${CSS.escape(entity.id)}"]`)
    : $(`.chat-row[data-conversation-id="${CSS.escape(entity.id)}"]`);
}
function navigationVisualElement(entity,source) {
  return entity.type==='project' ? source?.querySelector(':scope > .project-heading') : source;
}
function ensureNavigationPlaceholder(entity) {
  let placeholder = state.navigationDrag.placeholder;
  if (placeholder) return placeholder;
  const source = navigationSourceElement(entity);
  placeholder = document.createElement('div');
  placeholder.className = `navigation-drop-placeholder ${entity.type}`;
  placeholder.style.height = `${Math.max(36,source?.getBoundingClientRect().height||36)}px`;
  placeholder.setAttribute('aria-hidden','true');
  state.navigationDrag.placeholder = placeholder;
  return placeholder;
}
function navigationTargetKey(target) {
  return `${target.entityType}:${target.targetProjectId||'unfiled'}:${target.targetIndex}`;
}
function navigationOriginTarget(entity,source) {
  if(entity.type==='project'){
    const rows=[...source.parentElement.querySelectorAll(':scope > .project-block')];
    return {entityType:'project',targetProjectId:null,targetIndex:rows.indexOf(source),marker:source,position:'before'};
  }
  const rows=[...source.parentElement.querySelectorAll(':scope > .chat-row')];
  return {entityType:'conversation',targetProjectId:source.dataset.projectId||null,targetIndex:rows.indexOf(source),marker:source,position:'before'};
}
function createNavigationDragGhost(entity,source,{clientX,clientY,touch=false}={}) {
  const visual=navigationVisualElement(entity,source);if(!visual)return null;
  const rect=visual.getBoundingClientRect();const element=visual.cloneNode(true);
  element.removeAttribute('id');element.removeAttribute('draggable');
  element.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
  element.classList.add('navigation-drag-ghost');
  element.classList.toggle('touch',touch);
  element.style.width=`${rect.width}px`;document.body.append(element);
  const offsetX=Math.max(12,Math.min(rect.width-12,(clientX??rect.left+18)-rect.left));
  const offsetY=Math.max(12,Math.min(rect.height-12,(clientY??rect.top+18)-rect.top));
  state.navigationDrag.ghost={element,offsetX,offsetY,touch};
  if(touch)setNavigationDragGhostPosition(clientX,clientY);
  return state.navigationDrag.ghost;
}
function setNavigationDragGhostPosition(clientX,clientY) {
  const ghost=state.navigationDrag.ghost;if(!ghost?.touch)return;
  ghost.element.style.left=`${clientX-ghost.offsetX}px`;
  ghost.element.style.top=`${clientY-ghost.offsetY}px`;
}
function startNavigationLayout(entity) {
  const source=state.navigationDrag.sourceElement;if(!source)return;
  const origin=navigationOriginTarget(entity,source);const placeholder=ensureNavigationPlaceholder(entity);
  source.parentElement.insertBefore(placeholder,source);
  source.parentElement.classList.add('navigation-drop-active');
  state.navigationDrag.originTarget=origin;
  state.navigationDrag.target=origin;
  state.navigationDrag.placeholderKey=navigationTargetKey(origin);
  source.style.setProperty('--navigation-source-width',`${source.getBoundingClientRect().width}px`);
  requestAnimationFrame(()=>{
    if(state.navigationDrag.sourceElement===source&&state.navigationDrag.entity?.id===entity.id){
      source.classList.add('navigation-dragging','navigation-source-detached');
    }
  });
}
function navigationTargetContainer(target) {
  if (target.entityType === 'project') return $('#project-list');
  return target.targetProjectId
    ? $(`.project-block[data-project-id="${CSS.escape(target.targetProjectId)}"] .project-chats`)
    : $('#unfiled-list');
}
function navigationTargetRows(target, container) {
  const source = state.navigationDrag.entity;
  return target.entityType === 'project'
    ? [...container.querySelectorAll(':scope > .project-block')].filter(element => element.dataset.projectId !== source.id)
    : [...container.querySelectorAll(':scope > .chat-row')].filter(element => element.dataset.conversationId !== source.id);
}
function removeNavigationPlaceholder({animate=true}={}) {
  const placeholder = state.navigationDrag.placeholder;
  if (!placeholder?.isConnected) return;
  const parent = placeholder.parentElement;
  const remove = () => {
    placeholder.remove();
    parent?.classList.remove('navigation-drop-active');
  };
  if (animate) mutateNavigationLayout(remove); else remove();
  state.navigationDrag.placeholderKey = null;
}
function placeNavigationPlaceholder(target) {
  const entity = state.navigationDrag.entity;
  if (!entity || !target) return removeNavigationPlaceholder();
  const container = navigationTargetContainer(target);
  if (!container) return removeNavigationPlaceholder();
  const key = navigationTargetKey(target);
  if (state.navigationDrag.placeholderKey === key) return;
  const placeholder = ensureNavigationPlaceholder(entity);
  mutateNavigationLayout(() => {
    placeholder.parentElement?.classList.remove('navigation-drop-active');
    const rows = navigationTargetRows(target,container);
    container.insertBefore(placeholder,rows[target.targetIndex]||null);
    container.classList.add('navigation-drop-active');
  });
  state.navigationDrag.placeholderKey = key;
}
function activateNavigationPointerDrag(pointer,clientX,clientY) {
  if(pointer.active||state.navigationDrag.touch!==pointer)return;
  pointer.active=true;
  state.navigationDrag.entity=pointer.entity;
  state.navigationDrag.sourceElement=navigationSourceElement(pointer.entity);
  state.navigationDrag.dropping=false;
  createNavigationDragGhost(pointer.entity,state.navigationDrag.sourceElement,{clientX,clientY,touch:true});
  startNavigationLayout(pointer.entity);
  document.body.classList.add('navigation-touch-active');
  if(pointer.pointerType!=='mouse')navigator.vibrate?.(20);
}
function beginNavigationLongPress(event, entity, element) {
  if (state.navigationDrag.entity||state.navigationDrag.touch||element.classList.contains('pending-deletion')||(event.pointerType==='mouse'&&event.button!==0)) return;
  clearTimeout(state.navigationDrag.touch?.timer);
  const pointer={pointerId:event.pointerId,pointerType:event.pointerType,entity,element,active:false,startX:event.clientX,startY:event.clientY,timer:null};
  state.navigationDrag.touch=pointer;
  element.setPointerCapture?.(event.pointerId);
  if(event.pointerType!=='mouse')pointer.timer=setTimeout(()=>activateNavigationPointerDrag(pointer,pointer.startX,pointer.startY),480);
}
function handleNavigationPointerMove(event) {
  const touch=state.navigationDrag.touch;
  if(!touch||event.pointerId!==touch.pointerId)return;
  if(!touch.active){
    const distance=Math.hypot(event.clientX-touch.startX,event.clientY-touch.startY);
    if(touch.pointerType==='mouse'){
      if((event.buttons&1)===0){if(touch.element.hasPointerCapture?.(touch.pointerId))touch.element.releasePointerCapture(touch.pointerId);state.navigationDrag.touch=null;return;}
      if(distance<=4)return;
      activateNavigationPointerDrag(touch,event.clientX,event.clientY);
    }else if(distance>8){
      clearTimeout(touch.timer);
      if(touch.element.hasPointerCapture?.(touch.pointerId))touch.element.releasePointerCapture(touch.pointerId);
      state.navigationDrag.touch = null;
      return;
    }
    if(!touch.active)return;
  }
  event.preventDefault();
  setNavigationDragGhostPosition(event.clientX,event.clientY);
  const element = document.elementFromPoint(event.clientX,event.clientY);
  const target = element ? navigationDropTarget({target:element,clientY:event.clientY}) : null;
  if(target){state.navigationDrag.target=target;showNavigationDropTarget(target);}
}
async function handleNavigationPointerEnd(event) {
  const touch = state.navigationDrag.touch;
  if (!touch || event.pointerId !== touch.pointerId) return;
  clearTimeout(touch.timer);
  if(touch.element.hasPointerCapture?.(touch.pointerId))touch.element.releasePointerCapture(touch.pointerId);
  state.navigationDrag.touch = null;
  if (!touch.active) return;
  event.preventDefault();
  state.navigationDrag.suppressClickUntil = Date.now() + 700;
  const target = state.navigationDrag.target;
  if(event.type==='pointercancel'||!target)return rollbackNavigationDrag();
  if(navigationTargetKey(target)===navigationTargetKey(state.navigationDrag.originTarget))return finishNavigationDrag();
  state.navigationDrag.dropping=true;
  await commitNavigationDrop(touch.entity,target);
}
function clearNavigationDropMarkers() {
  $$('.navigation-drop-before,.navigation-drop-after,.navigation-drop-inside').forEach(element => {
    element.classList.remove('navigation-drop-before','navigation-drop-after','navigation-drop-inside');
  });
}
function staticTopWithin(element,container) {
  const documentTop=node=>{let top=0;for(let current=node;current;current=current.offsetParent)top+=current.offsetTop;return top;};
  return documentTop(element)-documentTop(container);
}
function stableNavigationInsertionIndex(container,rows,clientY) {
  const placeholder=state.navigationDrag.placeholder;
  if(placeholder?.parentElement===container&&state.navigationDrag.target){
    const rect=placeholder.getBoundingClientRect();
    if(clientY>=rect.top-6&&clientY<=rect.bottom+6)return state.navigationDrag.target.targetIndex;
  }
  const localY=clientY-container.getBoundingClientRect().top;
  let index=0;
  for(const row of rows){
    if(localY>=staticTopWithin(row,container)+row.offsetHeight/2)index+=1;
  }
  return index;
}
function navigationMarkerForIndex(container,rows,index) {
  if(rows[index])return {marker:rows[index],position:'before'};
  if(rows.length)return {marker:rows[rows.length-1],position:'after'};
  return {marker:container,position:'inside'};
}
function projectDropTarget(event) {
  const source = state.navigationDrag.entity;
  const container=event.target.closest('#project-list');
  if(!container||!source)return null;
  const rows=navigationTargetRows({entityType:'project'},container);
  const targetIndex=stableNavigationInsertionIndex(container,rows,event.clientY);
  return {entityType:'project',targetProjectId:null,targetIndex,...navigationMarkerForIndex(container,rows,targetIndex)};
}
function conversationDropTarget(event) {
  const source = state.navigationDrag.entity;
  if (!source) return null;
  let container=event.target.closest('.project-chats,#unfiled-list');
  if(!container){const block=event.target.closest('.project-block');container=block?.querySelector(':scope > .project-chats')||null;}
  if(!container)return null;
  const block=container.closest('.project-block');const targetProjectId=block?.dataset.projectId||null;
  const target={entityType:'conversation',targetProjectId};
  const rows=navigationTargetRows(target,container);
  const targetIndex=stableNavigationInsertionIndex(container,rows,event.clientY);
  return {...target,targetIndex,...navigationMarkerForIndex(container,rows,targetIndex)};
}
function navigationDropTarget(event) {
  const source = state.navigationDrag.entity;
  if (!source) return null;
  return source.type === 'project' ? projectDropTarget(event) : conversationDropTarget(event);
}
function showNavigationDropTarget(target) {
  clearNavigationDropMarkers();
  if (!target?.marker) return;
  target.marker.classList.add(
    target.position === 'before' ? 'navigation-drop-before'
      : target.position === 'after' ? 'navigation-drop-after'
        : 'navigation-drop-inside'
  );
  placeNavigationPlaceholder(target);
}
async function commitNavigationDrop(entity,target) {
  try {
    await api('/api/navigation/reorder', {
      method: 'POST',
      body: JSON.stringify({
        entity_type: entity.type,
        entity_id: entity.id,
        target_project_id: target.targetProjectId,
        target_index: target.targetIndex,
      }),
    });
    if (entity.type === 'conversation' && state.conversation?.conversation_id === entity.id) {
      state.conversation.project_id = target.targetProjectId;
    }
    const before = captureNavigationLayout();
    const placeholder=state.navigationDrag.placeholder;
    if(placeholder?.isConnected)before.set(`${entity.type}:${entity.id}`,placeholder.getBoundingClientRect());
    await refreshBootstrap({keepConversation:true});
    animateNavigationLayoutFrom(before);
    finishNavigationDrag();
  } catch (error) {
    toast(error.message, true);
    await rollbackNavigationDrag();
  }
}
async function rollbackNavigationDrag() {
  const source=state.navigationDrag.sourceElement;const placeholder=state.navigationDrag.placeholder;
  if(source?.isConnected&&placeholder?.isConnected){
    const originParent=source.parentElement;
    if(placeholder.parentElement!==originParent||placeholder.nextSibling!==source){
      mutateNavigationLayout(()=>{
        placeholder.parentElement?.classList.remove('navigation-drop-active');
        originParent.insertBefore(placeholder,source);
        originParent.classList.add('navigation-drop-active');
      });
      state.navigationDrag.placeholderKey=navigationTargetKey(state.navigationDrag.originTarget);
      if(!matchMedia('(prefers-reduced-motion: reduce)').matches)await sleep(200);
    }
  }
  finishNavigationDrag();
}
function finishNavigationDrag() {
  clearTimeout(state.navigationDrag.touch?.timer);
  const source=state.navigationDrag.sourceElement;
  source?.classList.remove('navigation-dragging','navigation-source-detached');
  source?.style.removeProperty('--navigation-source-width');
  state.navigationDrag.ghost?.element?.remove();
  clearNavigationDropMarkers();
  removeNavigationPlaceholder({animate:false});
  document.body.classList.remove('navigation-touch-active');
  state.navigationDrag.entity = null;
  state.navigationDrag.target = null;
  state.navigationDrag.originTarget = null;
  state.navigationDrag.touch = null;
  state.navigationDrag.sourceElement = null;
  state.navigationDrag.placeholder = null;
  state.navigationDrag.placeholderKey = null;
  state.navigationDrag.ghost = null;
  state.navigationDrag.dropping = false;
}
function fillProjectSelects() {
  const options = `<option value="">${t('unfiled')}</option>${(state.bootstrap.projects || []).map(p => `<option value="${p.project_id}">${escapeHtml(p.name)}</option>`).join('')}`;
  $('#request-project').innerHTML = options; $('#conversation-manage-project').innerHTML = options;
}
function projectName(id) { return state.bootstrap.projects.find(p => p.project_id === id)?.name || 'Unfiled'; }

function renderRequestBlock(key) {
  const spec = REQUEST_FIELDS[key]; if (!spec) return;
  const textarea = $(`#${spec.id}`);
  const preview = $(`[data-request-preview="${key}"]`);
  const hint = preview?.parentElement?.querySelector('.request-preview-hint');
  if (!textarea || !preview) return;
  const value = textarea.value.trim();
  textarea.classList.toggle('hidden', Boolean(value));
  preview.classList.toggle('hidden', !value);
  hint?.classList.toggle('hidden', !value);
  if (value) renderMarkdown(preview, value);
  else preview.replaceChildren();
}
function renderAllRequestBlocks() { Object.keys(REQUEST_FIELDS).forEach(renderRequestBlock); }
function openRequestEditor(key) {
  const spec = REQUEST_FIELDS[key]; if (!spec) return;
  state.requestEditor.key = key;
  $('#request-editor-source').value = $(`#${spec.id}`).value;
  renderMathText($('#request-editor-heading'), spec.heading);
  setSaveState('#request-editor-save-state', state.request.conversationId ? '已暂存' : '尚未保存到服务端');
  openDialog('#request-editor-dialog');
  scheduleRequestEditorPreview(true);
  requestAnimationFrame(() => $('#request-editor-source').focus({preventScroll:true}));
}
function scheduleRequestEditorPreview(immediate=false) {
  clearTimeout(state.requestEditor.previewTimer);
  state.requestEditor.previewTimer = setTimeout(() => renderMarkdown($('#request-editor-preview'), $('#request-editor-source').value), immediate ? 0 : 160);
}
function updateRequestFromEditor() {
  const spec = REQUEST_FIELDS[state.requestEditor.key]; if (!spec) return;
  $(`#${spec.id}`).value = $('#request-editor-source').value;
  if (state.request.conversationId) {
    setSaveState('#request-editor-save-state', '等待自动保存…', 'saving');
    scheduleRequestAutosave();
  }
}
async function finishRequestEditor() {
  const key = state.requestEditor.key; if (!key) return;
  clearTimeout(state.requestEditor.previewTimer);
  updateRequestFromEditor();
  if (state.request.conversationId && $('#request-problem').value.trim()) {
    clearTimeout(state.request.autosaveTimer);
    await saveRequestDraft({silent:true});
  }
  state.requestEditor.key = null;
  renderRequestBlock(key);
}

function resetRequestForm() {
  state.request = {conversationId: null, revision: 0, autosaveTimer: null, saving: false, sourceIds: new Set(), lastSavedFingerprint: null, pendingUploads: 0};
  for (const id of ['request-problem','request-background','request-methods','request-partial','request-ideas']) $(`#${id}`).value = '';
  $('#request-project').value = ''; $('#request-profile').value = 'high';
  $('#request-contract-language').value = state.bootstrap?.current_user?.default_contract_language||'zh';
  $('#request-dialog-title').textContent = t('requestTitle');
  setSaveState('#request-save-state', t('unsaved'));
  renderRequestGenerationEstimate();
  renderAllRequestBlocks();
  renderRequestFiles();
  updateRequestUploadState();
}
function prefillNewRequestFromCurrent() {
  const conversation = state.conversation;
  const snapshot = conversation?.request_snapshot;
  if (!snapshot) { toast('当前对话没有可复用的原始 Request',true); return; }
  resetRequestForm();
  $('#request-problem').value = snapshot.research_problem || '';
  $('#request-background').value = snapshot.background_and_prior_progress || '';
  $('#request-methods').value = snapshot.method_preferences_and_restrictions || '';
  $('#request-partial').value = snapshot.valuable_partial_outcomes || '';
  $('#request-ideas').value = snapshot.possible_ideas_and_rough_approaches || '';
  $('#request-profile').value = snapshot.contractor_profile || 'high';
  $('#request-contract-language').value = snapshot.contract_language || conversation.contract_language || state.bootstrap?.current_user?.default_contract_language || 'zh';
  const projectExists = (state.bootstrap.projects||[]).some(project => project.project_id === conversation.project_id);
  $('#request-project').value = projectExists ? conversation.project_id : '';
  const availableFiles = new Set((state.bootstrap.files||[]).map(file => file.file_id));
  const requestedFiles = snapshot.source_file_ids || [];
  state.request.sourceIds = new Set(requestedFiles.filter(fileId => availableFiles.has(fileId)));
  $('#request-dialog-title').textContent = uiLanguage()==='en'?'Create a request from this source':'基于此 Request 新建需求';
  setSaveState('#request-save-state','已预填；尚未保存到服务端');
  renderRequestGenerationEstimate();
  renderAllRequestBlocks();
  renderRequestFiles();
  openDialog('#new-contract-dialog');
  const missing = requestedFiles.filter(fileId => !availableFiles.has(fileId));
  if (missing.length) toast(`${missing.length} 个原始 Source Material 已不可用，未自动选中`,true);
}
function requestPayload() {
  return {
    research_problem: $('#request-problem').value.trim(),
    background_and_prior_progress: $('#request-background').value.trim(),
    method_preferences_and_restrictions: $('#request-methods').value.trim(),
    valuable_partial_outcomes: $('#request-partial').value.trim(),
    possible_ideas_and_rough_approaches: $('#request-ideas').value.trim(),
    contractor_profile: $('#request-profile').value,
    contract_language: $('#request-contract-language').value,
    project_id: $('#request-project').value || null,
    source_file_ids: [...state.request.sourceIds],
    expected_revision: state.request.revision,
  };
}
function requestFingerprint(payload=requestPayload()) {
  const stable={...payload,source_file_ids:[...(payload.source_file_ids||[])].sort()};
  delete stable.expected_revision;
  return JSON.stringify(stable);
}
function populateRequestDraft(detail) {
  const d = detail.request_draft;
  state.request = {conversationId: detail.conversation_id, revision: d.draft_revision, autosaveTimer: null, saving: false, sourceIds: new Set(d.source_file_ids || []), lastSavedFingerprint: null, pendingUploads: 0};
  $('#request-problem').value = d.research_problem || '';
  $('#request-background').value = d.background_and_prior_progress || '';
  $('#request-methods').value = d.method_preferences_and_restrictions || '';
  $('#request-partial').value = d.valuable_partial_outcomes || '';
  $('#request-ideas').value = d.possible_ideas_and_rough_approaches || '';
  $('#request-profile').value = d.contractor_profile || 'high';
  $('#request-contract-language').value = d.contract_language || detail.contract_language || 'zh';
  $('#request-project').value = detail.project_id || '';
  $('#request-dialog-title').textContent = detail.display_name || 'Research Request Draft';
  state.request.lastSavedFingerprint=requestFingerprint();
  setSaveState('#request-save-state', '已暂存');
  renderRequestGenerationEstimate();
  renderAllRequestBlocks();
  renderRequestFiles();
  updateRequestUploadState();
}
function renderRequestFiles() {
  const box = $('#request-files'); if (!box || !state.bootstrap) return;
  const files=new Map((state.bootstrap.files||[]).map(file=>[file.file_id,file]));
  box.replaceChildren();
  for(const fileId of state.request.sourceIds){
    const file=files.get(fileId);const row=document.createElement('div');row.className='request-file-row';
    const copy=document.createElement('span');const name=document.createElement('strong');name.textContent=file?.name||'资料已不可用';const size=document.createElement('small');size.textContent=file?formatBytes(file.size_bytes):'可从当前 Request 移除';copy.append(name,size);
    const remove=document.createElement('button');remove.type='button';remove.className='ghost danger-text compact-button';remove.dataset.requestRemoveFile=fileId;remove.textContent=t('remove');
    row.append(copy,remove);box.append(row);
  }
  if(!box.children.length)box.innerHTML='<p class="muted">尚未加入 Source Material。</p>';
  const library=$('#request-library-file');
  const available=(state.bootstrap.files||[]).filter(file=>!state.request.sourceIds.has(file.file_id));
  library.innerHTML=`<option value="">${available.length?t('selectUploaded'):t('noOtherFiles')}</option>${available.map(file=>`<option value="${escapeHtml(file.file_id)}">${escapeHtml(file.name)}</option>`).join('')}`;
  library.disabled=!available.length;
  $$('[data-request-remove-file]').forEach(button=>button.addEventListener('click',()=>removeRequestFile(button.dataset.requestRemoveFile)));
}
function removeRequestFile(fileId) {
  state.request.sourceIds.delete(fileId);renderRequestFiles();scheduleRequestAutosave();
}
function addLibraryFileToRequest(fileId) {
  if(!fileId)return;state.request.sourceIds.add(fileId);renderRequestFiles();scheduleRequestAutosave();
}
function updateRequestUploadState(message='',error=false) {
  const pending=Number(state.request.pendingUploads||0);const status=$('#request-upload-state');
  if(pending>0){status.textContent=`正在上传 ${pending} 个文件…`;status.classList.remove('hidden','error');}
  else if(message){status.textContent=message;status.classList.toggle('error',error);status.classList.remove('hidden');}
  else{status.textContent='';status.classList.add('hidden');status.classList.remove('error');}
  for(const button of [$('#save-request-draft'),$('#create-contract-submit')])button.disabled=pending>0||button.dataset.busy==='true';
}
function prepareUploadFiles(fileList) {
  const allowed=[];const rejected=[];
  for(const file of [...fileList]){
    if(!/\.(pdf|md|txt|tex)$/i.test(file.name))rejected.push(`${file.name}：不支持的文件类型`);
    else if(file.size>50*1024*1024)rejected.push(`${file.name}：超过 50 MiB`);
    else allowed.push(file);
  }
  rejected.forEach(message=>toast(message,true));
  return {allowed,rejected};
}
async function postUploadFiles(files) {
  return Promise.all(files.map(async file=>{const data=new FormData();data.append('file',file);try{return {file,item:await api('/api/files',{method:'POST',body:data})};}catch(error){return {file,error};}}));
}
function setupFileDropzone(dropzone,input,onFiles) {
  dropzone.addEventListener('click',event=>{if(event.target!==input)input.click();});
  dropzone.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();input.click();}});
  input.addEventListener('change',()=>{const files=[...(input.files||[])];input.value='';onFiles(files);});
  for(const name of ['dragenter','dragover'])dropzone.addEventListener(name,event=>{event.preventDefault();event.dataTransfer.dropEffect='copy';dropzone.classList.add('drag-active');});
  for(const name of ['dragleave','drop'])dropzone.addEventListener(name,event=>{event.preventDefault();if(name==='dragleave'&&dropzone.contains(event.relatedTarget))return;dropzone.classList.remove('drag-active');if(name==='drop')onFiles(event.dataTransfer.files);});
}
async function uploadRequestFiles(fileList) {
  const files=[...fileList];if(!files.length)return;
  const requestState=state.request;const ownerUserId=state.bootstrap?.current_user?.user_id;
  const {allowed,rejected}=prepareUploadFiles(files);
  if(!allowed.length){updateRequestUploadState(`${rejected.length} 个文件未上传`,true);return;}
  requestState.pendingUploads+=allowed.length;updateRequestUploadState();
  const results=await postUploadFiles(allowed);
  const successful=results.filter(result=>result.item);const failed=results.filter(result=>result.error);
  const stillCurrent=state.request===requestState&&state.bootstrap?.current_user?.user_id===ownerUserId;
  if(successful.length&&stillCurrent){
    const ids=new Set(successful.map(result=>result.item.file_id));
    state.bootstrap.files=[...successful.map(result=>result.item),...(state.bootstrap.files||[]).filter(file=>!ids.has(file.file_id))];
    successful.forEach(result=>requestState.sourceIds.add(result.item.file_id));
    renderRequestFiles();renderFileLibrary();scheduleRequestAutosave();
  }
  if(stillCurrent)failed.forEach(result=>toast(`${result.file.name}：${result.error.message}`,true));
  requestState.pendingUploads=Math.max(0,requestState.pendingUploads-allowed.length);
  if(stillCurrent)updateRequestUploadState(failed.length?`${failed.length} 个文件上传失败`:'',Boolean(failed.length));
}
function updateLibraryUploadState(message='',error=false) {
  const pending=Number(state.fileLibrary.pendingUploads||0);const status=$('#library-upload-state');
  if(pending>0){status.textContent=`正在上传 ${pending} 个文件…`;status.classList.remove('hidden','error');}
  else if(message){status.textContent=message;status.classList.toggle('error',error);status.classList.remove('hidden');}
  else{status.textContent='';status.classList.add('hidden');status.classList.remove('error');}
}
async function uploadLibraryFiles(fileList) {
  const files=[...fileList];if(!files.length)return;
  const libraryState=state.fileLibrary;const ownerUserId=state.bootstrap?.current_user?.user_id;
  const {allowed,rejected}=prepareUploadFiles(files);
  if(!allowed.length){updateLibraryUploadState(`${rejected.length} 个文件未上传`,true);return;}
  libraryState.pendingUploads+=allowed.length;updateLibraryUploadState();
  const results=await postUploadFiles(allowed);const successful=results.filter(result=>result.item);const failed=results.filter(result=>result.error);
  const stillCurrent=state.fileLibrary===libraryState&&state.bootstrap?.current_user?.user_id===ownerUserId;
  if(successful.length&&stillCurrent){
    const ids=new Set(successful.map(result=>result.item.file_id));
    state.bootstrap.files=[...successful.map(result=>result.item),...(state.bootstrap.files||[]).filter(file=>!ids.has(file.file_id))];
    renderFileLibrary();renderRequestFiles();
  }
  if(stillCurrent)failed.forEach(result=>toast(`${result.file.name}：${result.error.message}`,true));
  libraryState.pendingUploads=Math.max(0,libraryState.pendingUploads-allowed.length);
  if(stillCurrent)updateLibraryUploadState(failed.length?`${failed.length} 个文件上传失败`:'',Boolean(failed.length));
}
function scheduleRequestAutosave() {
  if (!state.request.conversationId) return;
  clearTimeout(state.request.autosaveTimer);
  setSaveState('#request-save-state','等待自动保存…','saving');
  state.request.autosaveTimer = setTimeout(() => saveRequestDraft({silent:true}), 800);
}
async function saveRequestDraft({silent=false} = {}) {
  if(state.request.pendingUploads>0){if(!silent)toast('请等待 Source Materials 上传完成',true);return null;}
  const payload = requestPayload();
  if (!payload.research_problem) { if (!silent) toast('“想研究的问题”不能为空', true); return null; }
  const fingerprint=requestFingerprint(payload);
  if(state.request.conversationId&&state.request.lastSavedFingerprint===fingerprint){
    setSaveState('#request-save-state','已暂存');
    if($('#request-editor-dialog').open)setSaveState('#request-editor-save-state','已暂存');
    return {conversation_id:state.request.conversationId,draft_revision:state.request.revision};
  }
  if (state.request.saving) return null;
  state.request.saving = true; setSaveState('#request-save-state','保存中…','saving');
  try {
    const path = state.request.conversationId ? `/api/contract-drafts/${state.request.conversationId}` : '/api/contract-drafts';
    const method = state.request.conversationId ? 'PATCH' : 'POST';
    const saved = await api(path,{method,body:JSON.stringify(payload)});
    state.request.conversationId = saved.conversation_id; state.request.revision = saved.draft_revision;
    state.request.lastSavedFingerprint=fingerprint;
    setSaveState('#request-save-state','已暂存');
    if ($('#request-editor-dialog').open) setSaveState('#request-editor-save-state','已暂存');
    await refreshBootstrap({keepConversation:true});
    if (!silent) toast('Research Request Draft 已暂存');
    return saved;
  } catch (error) {
    setSaveState('#request-save-state',error.message,'error');
    if ($('#request-editor-dialog').open) setSaveState('#request-editor-save-state',error.message,'error');
    if (!silent) toast(error.message,true);
    return null;
  } finally { state.request.saving = false; }
}
async function generateFromRequest(event) {
  event.preventDefault();
  if(state.request.pendingUploads>0){toast('请等待 Source Materials 上传完成',true);return;}
  await withBusy(event.submitter||$('#create-contract-submit'),async()=>{
    const saved = await saveRequestDraft({silent:true}); if (!saved) return;
    try {
      await api(`/api/conversations/${saved.conversation_id}/generate`,{method:'POST',body:JSON.stringify({idempotency_key:uid('contract-generate')})});
      closeDialog('#new-contract-dialog');
      state.generationNoticeClosed=false; $('#generation-notice').classList.remove('hidden');
      await refreshBootstrap({keepConversation:true});
      await openConversation(saved.conversation_id,{showLoading:false});
      toast('Research Contract 已进入后台生成队列');
    } catch (error) { showAlert('生成 Research Contract 失败',error.message); }
  });
}
async function openNewContract() { resetRequestForm(); openDialog('#new-contract-dialog'); }
async function openRequestDraftConversation(detail) { populateRequestDraft(detail); openDialog('#new-contract-dialog'); }

async function openConversation(id,{showLoading=true}={}) {
  rememberContractScroll();
  rememberContractSelection();
  stopRunPolling();
  state.viewingRunContract=false;
  try {
    state.conversation = await api(`/api/conversations/${id}`);
    if (state.conversation.status === 'request_draft' && state.conversation.request_draft) {
      renderNavigation(); await openRequestDraftConversation(state.conversation); return;
    }
    chooseDefaultContractView(); renderNavigation(); await renderConversation();
    if (state.conversation.run) await openRun(state.conversation.run.run_id,true);
  } catch (error) { toast(error.message,true); }
}
function chooseDefaultContractView() {
  const c = state.conversation;
  const pendingVersion = pendingContractVersion();
  if (pendingVersion) {
    setContractSelection({kind:'pending',version:pendingVersion});
    return;
  }
  const remembered = state.contractSelections.get(c.conversation_id);
  if (remembered?.kind === 'draft' && c.working_draft) {
    state.selected = {...remembered};
    return;
  }
  if (remembered?.kind === 'version' && c.versions.some(v => Number(v.version) === Number(remembered.version))) {
    state.selected = {...remembered};
    return;
  }
  if (remembered?.kind === 'request' && c.request_snapshot) {
    state.selected = {...remembered};
    return;
  }
  setContractSelection(c.working_draft ? {kind:'draft',version:null} : {kind:'version',version:c.latest_contract_version || null});
}
function rememberContractSelection() {
  const conversationId = state.conversation?.conversation_id;
  if (!conversationId) return;
  state.contractSelections.set(conversationId, {...state.selected});
}
function setContractSelection(selection) {
  state.selected = {...selection};
  rememberContractSelection();
}
function selectedContractData() {
  const c = state.conversation;
  if (!c) return null;
  if (state.selected.kind === 'request') return c.request_snapshot ? {...c.request_snapshot,kind:'request',title:'Original Research Request'} : null;
  if (state.selected.kind === 'draft') return c.working_draft ? {...c.working_draft,kind:'draft'} : null;
  const version = c.versions.find(v => v.version === Number(state.selected.version));
  return version ? {...version,kind:'version'} : null;
}
function selectedChatBranch() {
  const data=selectedContractData();if(!data)return null;
  if(data.kind==='request')return null;
  if(data.kind==='draft'){
    const base=state.conversation.versions.find(item=>Number(item.version)===Number(data.base_version));
    return base?{contractVersionId:base.contract_version_id,selectedVersion:Number(base.version)}:null;
  }
  return {contractVersionId:data.contract_version_id,selectedVersion:Number(data.version)};
}
function activeContractGeneration() {
  const generation=state.conversation?.generation;
  return generation&&['queued','running','repairing'].includes(generation.status)?generation:null;
}
function pendingContractGeneration() {
  const generation=state.conversation?.generation;
  return generation&&['queued','running','repairing','interrupted'].includes(generation.status)?generation:null;
}
function pendingContractVersion() {
  const generation = pendingContractGeneration();
  if (!generation) return null;
  return Number(generation.target_version || (Number(state.conversation?.latest_contract_version || 0) + 1));
}
function isConversationLocked() { return ['contract_generating','generation_interrupted','execution_locked','running','completed'].includes(state.conversation?.status); }
function contractViewKey() {
  if (!state.conversation) return null;
  const view = state.selected.kind === 'draft' ? 'draft' : `${state.selected.kind}:${state.selected.version}`;
  return `${state.conversation.conversation_id}:${view}`;
}
function rememberContractScroll() { const key=contractViewKey(); if(key)state.contractScroll.set(key,$('#contract-main').scrollTop); }
function restoreContractScroll() { const value=state.contractScroll.get(contractViewKey())||0; requestAnimationFrame(()=>{$('#contract-main').scrollTop=value;scheduleContractOutlineUpdate(true);}); }
function splitNodeFragments(source,key) {
  const text=String(source||'');
  const detailed=key==='detailed_horizon';
  const expression=detailed?/^###\s+(RN-\d+)\s*[（(]\s*(Discovery|Claim)\s*[）)]\s*[:：]\s*(.+?)\s*$/gim:/^###\s+((?:RD|SD)-\d+)\s*[:：]\s*(.+?)\s*$/gim;
  const matches=[...text.matchAll(expression)];
  if(!matches.length)return {intro:text,fragments:[]};
  return {
    intro:text.slice(0,matches[0].index).trim(),
    fragments:matches.map((match,index)=>{
      const start=match.index;const end=index+1<matches.length?matches[index+1].index:text.length;
      const id=match[1].toUpperCase();const kind=detailed?match[2]:null;const title=(detailed?match[3]:match[2]).trim();
      return {index,start,end,id,kind,title,displayTitle:kind?`${id}（${kind}）：${title}`:`${id}：${title}`,heading:match[0],source:text.slice(start,end).trimEnd(),body:text.slice(start+match[0].length,end).trim()};
    }),
  };
}
function addOutlineButton(row,target,label,major) {
  const button=document.createElement('button');button.type='button';button.dataset.outlineTarget=target;button.dataset.outlineMajor=major;button.textContent=label;
  button.onclick=()=>document.getElementById(target)?.scrollIntoView({behavior:'smooth',block:'start'});row.append(button);
}
function renderContractMap(){
  const map=$('#contract-map');const nodes=$('#contract-map-nodes');nodes.replaceChildren();
  for(const key of ['A','B','C','D','E','F','G']){
    const target=key==='G'?'execution-policy':`contract-section-${key}`;const button=document.createElement('button');button.type='button';button.className='contract-map-node';button.dataset.section=key;button.dataset.outlineMajor=key;button.dataset.outlineTarget=target;
    const title=document.createElement('strong');title.textContent=CONTRACT_MAJOR_LABELS[key].replace(/^. · /,'');const copy=document.createElement('small');copy.textContent=CONTRACT_MAP_DESCRIPTIONS[uiLanguage()][key];button.append(title,copy);button.onclick=()=>document.getElementById(target)?.scrollIntoView({behavior:'smooth',block:'start'});nodes.append(button);
  }
  map.classList.remove('hidden');
}
function cacheContractOutlinePositions() {
  const main=$('#contract-main');const mainTop=main.getBoundingClientRect().top;
  const anchors=$$('#contract-sections [data-outline-major]');const execution=$('#execution-policy');if(!execution.classList.contains('hidden'))anchors.push(execution);
  state.contractOutline.positions=anchors.map(anchor=>({anchor,top:anchor.getBoundingClientRect().top-mainTop+main.scrollTop}));
  state.contractOutline.rebuild=false;
}
function updateContractOutline() {
  const main=$('#contract-main');const positions=state.contractOutline.positions;if(!positions.length)return;
  const map=$('#contract-map');const outline=$('#contract-outline');const compactThreshold=map.offsetTop+map.offsetHeight-36;outline.classList.toggle('compact-visible',main.scrollTop>compactThreshold);
  const top=main.scrollTop+125;
  let active=positions[0].anchor;
  for(const item of positions){if(item.top<=top)active=item.anchor;else break;}
  const major=active.dataset.outlineMajor;const minor=active.dataset.outlineMinor||'';
  $$('#contract-outline-major button').forEach(button=>button.classList.toggle('active',button.dataset.outlineMajor===major));
  $$('#contract-map-nodes button').forEach(button=>button.classList.toggle('active',button.dataset.outlineMajor===major));
  $$('#contract-outline-minor button').forEach(button=>{button.classList.toggle('hidden',button.dataset.outlineMajor!==major);button.classList.toggle('active',button.dataset.outlineTarget===minor);});
  $('#contract-outline-minor').classList.toggle('hidden',!$$('#contract-outline-minor button').some(button=>!button.classList.contains('hidden')));
}
function scheduleContractOutlineUpdate(rebuild=false) {
  state.contractOutline.rebuild=state.contractOutline.rebuild||rebuild;
  if(state.contractOutline.frame)return;
  state.contractOutline.frame=requestAnimationFrame(()=>{
    state.contractOutline.frame=null;
    if(state.contractOutline.rebuild)cacheContractOutlinePositions();
    updateContractOutline();
  });
}
async function renderConversation() {
  const c = state.conversation; if (!c) return;
  showView('conversation');
  renderMathText($('#breadcrumb'), c.project_id ? projectName(c.project_id) : 'Unfiled');
  renderMathText($('#page-title'), c.display_name);
  $('#back-to-run').classList.toggle('hidden',!(state.viewingRunContract&&state.run?.conversation_id===c.conversation_id));
  const select = $('#version-select');
  const opts = c.request_snapshot ? ['<option value="request">Request</option>'] : [];
  opts.push(...c.versions.map(v => `<option value="version:${v.version}">Contract v${v.version}</option>`));
  const pendingVersion = pendingContractVersion();
  if (pendingVersion) opts.push(`<option value="pending:${pendingVersion}">Contract v${pendingVersion} · Pending</option>`);
  if (c.working_draft) opts.push(`<option value="draft">Working Draft · based on v${c.working_draft.base_version}</option>`);
  select.innerHTML = opts.join('') || '<option>尚无 Contract</option>';
  select.disabled = !opts.length;
  select.value = state.selected.kind === 'draft' ? 'draft' : (state.selected.kind === 'request' ? 'request' : `${state.selected.kind}:${state.selected.version}`);
  $('#draft-badge').classList.toggle('hidden', !c.working_draft);
  const viewingRequest=state.selected.kind==='request';
  $('#save-contract-version').classList.toggle('hidden', !(c.working_draft && state.selected.kind === 'draft') || isConversationLocked());
  $('#discard-working-draft').classList.toggle('hidden', !c.working_draft || isConversationLocked() || viewingRequest);
  $('#clone-contract').textContent = viewingRequest ? (uiLanguage()==='en'?'Branch from Request':'从此 Request 新开需求') : t('cloneContract');
  $('#clone-contract').disabled = viewingRequest ? !c.request_snapshot : !c.versions.length;
  const regenerationActive=Boolean(activeContractGeneration());
  $('#full-regenerate').disabled = !c.versions.length || isConversationLocked() || regenerationActive || viewingRequest;
  $('#full-regenerate').textContent = regenerationActive ? (uiLanguage()==='en'?'Regenerating…':'正在重新规划…') : t('fullRegenerate');
  $('#full-regenerate').title = regenerationActive ? '正在重新规划中，该功能暂不可用' : '';
  $('#contract-chat-input').disabled = !c.versions.length || isConversationLocked() || viewingRequest;
  $('#contract-chat-form button[type="submit"]').disabled = !c.versions.length || isConversationLocked() || viewingRequest;
  $('#contract-sources').disabled = isConversationLocked() || viewingRequest;
  await renderGenerationPanel();
  await renderContract();
  await renderContractChat();
  startGenerationPollingIfNeeded();
}
async function renderGenerationPanel() {
  const g = state.conversation.generation;
  const active = g && ['queued','running','repairing'].includes(g.status);
  if (!g) return;
  const regenerate=$('#full-regenerate');
  if(regenerate){const regenerating=active&&g.kind==='full_regeneration';regenerate.disabled=regenerate.disabled||regenerating;regenerate.textContent=regenerating?'正在重新规划…':'完整重新生成新版本';regenerate.title=regenerating?'正在重新规划中，该功能暂不可用':'';}
  const otherGenerating=(state.bootstrap?.conversations||[]).some(item=>item.status==='contract_generating'&&item.conversation_id!==state.conversation?.conversation_id);
  $('#generation-notice').classList.toggle('hidden',!otherGenerating||state.generationNoticeClosed);
}
const generationPhaseCopy = {
  contractor: {
    waiting: '正在等待 Contract 生成任务进入执行队列',
    running: '正在分析 Research Request，并撰写初版 Research Contract',
    completed: '已完成初版 Contract，已提交给三名独立 Auditor',
    interrupted: 'Contractor 执行已中断，可以从原 thread 继续',
  },
  auditors: {
    waiting: '正在等待前一步交付 Contract 初稿',
    running: '三名独立 Auditor 正在仔细审核初版 Contract 提及的方向',
    completed: '已完成初步审核，已将审核意见提交给最终 Contract Integrator',
    interrupted: '审核阶段已中断；继续后只恢复尚未完成的审核',
  },
  integrator: {
    waiting: '正在等待 Auditors 完成审核',
    running: '正在综合审核意见，并生成最终 Research Contract',
    completed: '已完成整合，Research Contract 已生成',
    interrupted: '最终整合已中断，可以从原 thread 继续',
  },
};
const generationPhaseCopyEn = {
  contractor:{waiting:'Waiting for Contract generation to enter the queue',running:'Analyzing the Research Request and drafting the initial Research Contract',completed:'Initial Contract completed and sent to three independent Auditors',interrupted:'Contractor execution was interrupted and can resume in the same thread'},
  auditors:{waiting:'Waiting for the initial Contract draft',running:'Three independent Auditors are reviewing the draft',completed:'Audit reports completed and sent to the Contract Integrator',interrupted:'Auditing was interrupted; only unfinished reviews will resume'},
  integrator:{waiting:'Waiting for the Auditors',running:'Integrating the audit reports into the final Research Contract',completed:'The integrated Research Contract is complete',interrupted:'Integration was interrupted and can resume in the same thread'},
};
const generationStatusLabel = {waiting:'等待中',running:'运行中',completed:'已完成',interrupted:'已中断'};
const generationStatusLabelEn = {waiting:'Waiting',running:'Running',completed:'Completed',interrupted:'Interrupted'};
function formatGenerationDuration(value){const seconds=Math.max(0,Math.floor(Number(value)||0));const hours=Math.floor(seconds/3600);const minutes=Math.floor(seconds%3600/60);if(uiLanguage()==='en')return hours?`${hours}h ${minutes}m`:`${minutes}m`;return hours?`${hours}小时${minutes}分钟`:`${minutes}分钟`;}
function updateGenerationElapsed(){const target=$('#generation-elapsed-value');if(!target||!state.generationElapsedAttemptId)return;const extra=Math.max(0,Math.floor((Date.now()-state.generationElapsedBaseAt)/1000));const duration=formatGenerationDuration(state.generationElapsedBaseSeconds+extra);target.textContent=uiLanguage()==='en'?`Contract generation elapsed: ${duration}`:`Contract 生成已进行 ${duration}`;}
function stopGenerationElapsedTimer(){if(state.generationElapsedTimer)clearInterval(state.generationElapsedTimer);state.generationElapsedTimer=null;state.generationElapsedAttemptId=null;state.generationElapsedBaseSeconds=0;state.generationElapsedBaseAt=0;}
function syncGenerationElapsed(generation){state.generationElapsedAttemptId=String(generation.generation_attempt_id||'');state.generationElapsedBaseSeconds=Math.max(0,Number(generation.elapsed_seconds)||0);state.generationElapsedBaseAt=Date.now();updateGenerationElapsed();if(!state.generationElapsedTimer)state.generationElapsedTimer=setInterval(updateGenerationElapsed,1000);}
function renderGenerationWorkflow(generation) {
  const workflow=$('#generation-workflow');
  const visible=Boolean(generation&&['queued','running','repairing','interrupted'].includes(generation.status));
  workflow.classList.toggle('hidden',!visible);
  $('#contract-empty-default').classList.toggle('hidden',visible);
  $('#contract-empty').classList.toggle('workflow-active',visible);
  if(!visible){stopGenerationElapsedTimer();return;}
  syncGenerationElapsed(generation);
  const phases=new Map((generation.phases||[]).map(item=>[item.key,item]));
  for(const key of ['contractor','auditors','integrator']){
    const phase=phases.get(key)||{key,status:'waiting',completed:0,total:key==='auditors'?3:1};
    const row=workflow.querySelector(`[data-generation-phase="${key}"]`);
    row.classList.remove('waiting','running','completed','interrupted');row.classList.add(phase.status);
    const statusLabels=uiLanguage()==='en'?generationStatusLabelEn:generationStatusLabel;const phaseCopy=uiLanguage()==='en'?generationPhaseCopyEn:generationPhaseCopy;row.querySelector('.generation-phase-status').textContent=statusLabels[phase.status]||statusLabels.waiting;
    let copy=phaseCopy[key][phase.status]||phaseCopy[key].waiting;
    if(key==='auditors'&&['running','interrupted'].includes(phase.status))copy+=uiLanguage()==='en'?` · ${phase.completed||0}/${phase.total||3} completed`:` · 已完成 ${phase.completed||0}/${phase.total||3}`;
    row.querySelector('.generation-phase-copy').textContent=copy;
  }
  const resumable=Boolean(generation.resumable&&generation.status==='interrupted');
  $('#generation-resume-actions').classList.toggle('hidden',!resumable);
  $('#generation-interruption-copy').textContent=resumable?(uiLanguage()==='en'?'Completed stages will be reused exactly; resuming will not restart those Agents.':'已完成阶段会原样复用；继续执行不会重新启动这些 Agent。'):'';
  $('#resume-contract-generation').disabled=!resumable;
}
async function renderContract() {
  const data = selectedContractData();
  const mathEntries=[];const markdownEntries=[];
  const pendingVersion = state.selected.kind === 'pending' ? pendingContractVersion() : null;
  renderGenerationWorkflow(pendingVersion?state.conversation.generation:null);
  $('#contract-empty').classList.toggle('hidden', Boolean(data));
  $('#contract-sections').replaceChildren();
  $('#contract-outline-major').replaceChildren();
  $('#contract-outline-minor').replaceChildren();
  $('#contract-map-nodes').replaceChildren();
  $('#contract-outline').classList.remove('compact-visible');
  state.contractOutline.positions=[];
  const viewingRequest=data?.kind==='request';
  $('#contract-outline').classList.toggle('hidden', !data || viewingRequest);
  $('#contract-map').classList.toggle('hidden', !data || viewingRequest);
  $('#execution-policy').classList.toggle('hidden', !data || viewingRequest);
  $('#approval-zone').classList.toggle('hidden', !data || viewingRequest || isConversationLocked());
  if (!data) return;
  if(viewingRequest){await renderRequestSnapshot(data);restoreContractScroll();return;}
  const title = document.createElement('div'); title.className='contract-title';
  const h1=document.createElement('h1'); h1.textContent=data.title || 'Research Contract'; title.append(h1); $('#contract-sections').append(title); mathEntries.push({container:h1,source:data.title||'Research Contract'});
  const editable = new Set(state.conversation.editable_section_keys || []);
  let currentMajor='';
  for (const spec of state.bootstrap.application.contract_sections) {
    if (['mathematical_contract','research_policy'].includes(spec.key)) {
      currentMajor=spec.heading.slice(0,1);
      const group=document.createElement('div'); group.className='contract-group-heading'; group.dataset.outlineMajor=currentMajor; group.id=`contract-section-${currentMajor}`;
      const heading=document.createElement(spec.level===2?'h2':'h3'); heading.textContent=spec.heading; group.append(heading); $('#contract-sections').append(group); mathEntries.push({container:heading,source:spec.heading}); continue;
    }
    const nodeSection=['detailed_horizon','structured_horizon'].includes(spec.key);const sectionEditable=editable.has(spec.key)&&!isConversationLocked()&&!nodeSection;
    const section=document.createElement('section'); section.className=`contract-section${sectionEditable?' editable':''}`; section.dataset.sectionKey=spec.key; section.dataset.editable=sectionEditable?'true':'false';
    if(spec.level===2){currentMajor=spec.heading.slice(0,1);section.dataset.outlineMajor=currentMajor;section.id=`contract-section-${currentMajor}`;}
    else if(['A','B'].includes(currentMajor)){const childId=`contract-subsection-${spec.key}`;section.id=childId;section.dataset.outlineMajor=currentMajor;section.dataset.outlineMinor=childId;}
    const heading=document.createElement(spec.level===2?'h2':'h3'); heading.textContent=spec.heading;
    section.append(heading); $('#contract-sections').append(section); mathEntries.push({container:heading,source:spec.heading});
    const source=data.sections?.[spec.key] || '';
    if(['detailed_horizon','structured_horizon'].includes(spec.key)){
      const split=splitNodeFragments(source,spec.key);
      if(split.intro){const intro=document.createElement('div');intro.className='markdown contract-subsection-intro';section.append(intro);markdownEntries.push({container:intro,source:split.intro});}
      const list=document.createElement('div');list.className='contract-subsection-list';section.append(list);
      for(const fragment of split.fragments){
        const card=document.createElement('article');const childId=`contract-${currentMajor}-node-${fragment.id.toLowerCase()}`;card.id=childId;card.className=`contract-subsection-card${editable.has(spec.key)&&!isConversationLocked()?' editable':''}`;card.dataset.sectionKey=spec.key;card.dataset.fragmentId=fragment.id;card.dataset.editable=editable.has(spec.key)&&!isConversationLocked()?'true':'false';card.dataset.outlineMajor=currentMajor;card.dataset.outlineMinor=childId;
        const nodeHeading=document.createElement('h3');nodeHeading.textContent=fragment.displayTitle;const body=document.createElement('div');body.className='markdown';card.append(nodeHeading,body);list.append(card);mathEntries.push({container:nodeHeading,source:fragment.displayTitle});markdownEntries.push({container:body,source:fragment.body});
      }
      if(!split.fragments.length){const body=document.createElement('div');body.className='markdown';section.append(body);markdownEntries.push({container:body,source});}
    }else{const body=document.createElement('div');body.className='markdown';section.append(body);markdownEntries.push({container:body,source});}
  }
  await renderMathTextBatch(mathEntries);
  await renderMarkdownBatch(markdownEntries);
  renderExecutionPolicy(state.conversation.execution_policy || {});
  renderContractMap();
  for(const key of ['A','B','C','D','E','F','G'])addOutlineButton($('#contract-outline-major'),key==='G'?'execution-policy':`contract-section-${key}`,CONTRACT_MAJOR_LABELS[key],key);
  for(const anchor of $$('#contract-sections [data-outline-minor]')){
    const label=anchor.classList.contains('contract-subsection-card')?anchor.querySelector('h3')?.textContent:anchor.querySelector('h3')?.textContent;
    if(label)addOutlineButton($('#contract-outline-minor'),anchor.dataset.outlineMinor,label,anchor.dataset.outlineMajor);
  }
  restoreContractScroll();
  $('#approval-copy').textContent=t('lockCopy');
}
async function renderRequestSnapshot(data){
  const mathEntries=[];const markdownEntries=[];const title=document.createElement('div');title.className='contract-title';const h1=document.createElement('h1');h1.textContent='Original Research Request';title.append(h1);$('#contract-sections').append(title);mathEntries.push({container:h1,source:'Original Research Request'});
  const fields=[
    ['1. 想研究的问题','research_problem'],
    ['2. 已知背景 / 已有进展 / 失败路线','background_and_prior_progress'],
    ['3. 方法偏好与限制','method_preferences_and_restrictions'],
    ['4. 完整目标做不出时仍值得保留的结果','valuable_partial_outcomes'],
    ['5. 可能的想法或大致做法','possible_ideas_and_rough_approaches'],
  ];
  for(const [heading,key] of fields){const section=document.createElement('section');section.className='contract-section request-snapshot-section';const h2=document.createElement('h2');h2.textContent=heading;const body=document.createElement('div');body.className='markdown';section.append(h2,body);$('#contract-sections').append(section);mathEntries.push({container:h2,source:heading});markdownEntries.push({container:body,source:data[key]||'未填写。'});}
  const sources=Array.isArray(data.source_manifest)?data.source_manifest:[];const section=document.createElement('section');section.className='contract-section request-snapshot-section';const h2=document.createElement('h2');h2.textContent='Source Materials';const list=document.createElement('div');list.className='markdown';const names=sources.map(item=>item.original_name||item.name||item.file_id).filter(Boolean);section.append(h2,list);$('#contract-sections').append(section);mathEntries.push({container:h2,source:'Source Materials'});markdownEntries.push({container:list,source:names.length?names.map(name=>`- ${name}`).join('\n'):'未附加文件。'});
  await renderMathTextBatch(mathEntries);await renderMarkdownBatch(markdownEntries);
}
function renderDeploymentOptions(container,keys,selected,kind,editable){
  container.replaceChildren();container.dataset.value=selected;
  for(const key of keys){
    const button=document.createElement('button');button.type='button';button.className=`deployment-option deployment-${kind}-option${key==='qwen_only'?' reserve':''}`;button.dataset.tier=key;button.setAttribute('role','radio');button.setAttribute('aria-checked',String(key===selected));button.disabled=!editable;
    if(kind==='model'){
      const meta=DEPLOYMENT_MODEL_META[key]||{subtitleLines:['Research','Configuration'],icon:''};const label=EXECUTION_MODEL_LABELS[key]||key;const subtitle=meta.subtitleLines.join('，');button.setAttribute('aria-label',`${label}，${subtitle}`);button.innerHTML=`<span class="deployment-option-icon">${meta.icon}</span><span class="deployment-option-copy"><strong>${label}</strong><small>${meta.subtitleLines.map(line=>`<span>${line}</span>`).join('')}</small></span><span class="deployment-option-check">${DEPLOYMENT_CHECK_ICON}</span>`;
    }else{
      const duration=state.bootstrap.execution_policy_catalog?.duration_tiers?.[key];const minutes=Math.round(Number(duration?.duration_seconds||0)/60);const label=EXECUTION_DURATION_LABELS[key]||key;button.setAttribute('aria-label',`${label}，约 ${minutes} 分钟`);button.innerHTML=`<span class="deployment-option-copy"><strong>${label}</strong><small>≈ ${minutes} min</small></span><span class="deployment-option-check">${DEPLOYMENT_CHECK_ICON}</span>`;
    }
    button.onclick=()=>selectExecutionTier(kind,key);container.append(button);
  }
}
function syncDeploymentOptionStates(container){for(const button of container.querySelectorAll('[data-tier]'))button.setAttribute('aria-checked',String(button.dataset.tier===container.dataset.value));}
function updateExecutionSlogan(model,duration){
  const stage=$('#execution-slogan-stage');const quote=$('#execution-slogan');const attributionNode=$('#execution-slogan-attribution');const key=`${model}:${duration}:${uiLanguage()}`;const english=EXECUTION_QUOTES_EN[model]?.[duration];const slogan=uiLanguage()==='en'?(english?.text||'Prepare the next research stage.'):(EXECUTION_SLOGANS[model]?.[duration]||'准备开始下一轮研究。');const attribution=uiLanguage()==='en'?(english?.attribution||''):(EXECUTION_SLOGAN_ATTRIBUTIONS[model]?.[duration]||'');
  const background=DEPLOYMENT_BACKGROUNDS[model]?.[duration]||DEPLOYMENT_BACKGROUNDS.gpt_medium.balanced_3h;
  const apply=()=>{quote.textContent=slogan;attributionNode.textContent=attribution;attributionNode.classList.toggle('hidden',!attribution);stage.style.setProperty('--deployment-background-image',`url('${background}')`);stage.dataset.sloganKey=key;};
  if(state.deploymentSloganTimer){clearTimeout(state.deploymentSloganTimer);state.deploymentSloganTimer=null;}
  if(stage.dataset.sloganKey===key){stage.classList.remove('is-changing');apply();return;}
  if(!stage.dataset.sloganKey){apply();return;}
  stage.classList.add('is-changing');
  state.deploymentSloganTimer=setTimeout(()=>{apply();stage.classList.remove('is-changing');state.deploymentSloganTimer=null;},140);
}
function renderExecutionPolicy(policy) {
  const catalog=state.bootstrap.execution_policy_catalog||{};const allModels=catalog.model_tiers||{};const allDurations=catalog.duration_tiers||{};
  const modelKeys=SELECTABLE_EXECUTION_MODEL_TIERS.filter(key=>allModels[key]);const durationKeys=SELECTABLE_EXECUTION_DURATION_TIERS.filter(key=>allDurations[key]);
  const defaultModel=normalizedExecutionModelTier(state.bootstrap.current_user.default_model_strategy);const defaultDuration=normalizedExecutionDurationTier(state.bootstrap.current_user.default_budget_preset);
  const requestedModel=normalizedExecutionModelTier(policy.model_tier||defaultModel);const model=modelKeys.includes(requestedModel)?requestedModel:modelKeys[0];
  const requestedDuration=normalizedExecutionDurationTier(policy.duration_tier||defaultDuration);const duration=durationKeys.includes(requestedDuration)?requestedDuration:durationKeys[0];
  const editable=!isConversationLocked();const contract=selectedContractData();$('#deployment-contract-version').textContent=contract?.kind==='draft'?'WORKING DRAFT':(contract?.version?`CONTRACT v${contract.version}`:'CONTRACT');renderDeploymentOptions($('#execution-model-tier'),modelKeys,model,'model',editable);renderDeploymentOptions($('#execution-duration-tier'),durationKeys,duration,'duration',editable);
  const languageSelect=$('#execution-research-language');const conversationKey=state.conversation?.conversation_id||'';
  if(languageSelect.dataset.conversationId!==conversationKey){languageSelect.dataset.conversationId=conversationKey;languageSelect.value=state.conversation?.run?.research_language||state.bootstrap.current_user.default_research_language||'zh';}
  if(state.conversation?.run?.research_language)languageSelect.value=state.conversation.run.research_language;
  languageSelect.disabled=!editable;updateExecutionEstimate();
}
function currentCatalogPolicy() {
  const model=$('#execution-model-tier').dataset.value;const duration=$('#execution-duration-tier').dataset.value;const catalog=state.bootstrap.execution_policy_catalog||{};
  return {model_tier:model,duration_tier:duration,estimated_cost:Number(catalog.price_table?.[model]?.[duration]||0),pricing_version:catalog.pricing_version||'unknown',duration_seconds:Number(catalog.duration_tiers?.[duration]?.duration_seconds||0),publication_estimate_seconds:Number(catalog.publication_estimate_seconds||2700)};
}
function updateExecutionEstimate(policy=null) {
  const p=policy?.model_tier?policy:currentCatalogPolicy();const catalog=state.bootstrap.execution_policy_catalog||{};const catalogPrice=catalog.price_table?.[p.model_tier]?.[p.duration_tier];const estimate=catalogPrice==null?Number(p.estimated_cost||0):Number(catalogPrice);const modelInfo=catalog.model_tiers?.[p.model_tier]||{};
  const researchSeconds=Number(catalog.duration_tiers?.[p.duration_tier]?.duration_seconds||p.duration_seconds||0);const publicationSeconds=Number(p.publication_estimate_seconds||catalog.publication_estimate_seconds||2700);const totalSeconds=researchSeconds+publicationSeconds;
  const englishDescriptions={gpt_low:'A cost-effective GPT route for quickly testing feasibility and the main research direction.',gpt_medium:'A balanced route using high-intensity reasoning on the critical path and lighter models for parallel exploration.',gpt_high:'The highest-intensity GPT route for the strongest available research quality.',qwen_only:'A slower fallback route intended mainly for periods when GPT models are unavailable.'};
  const modelLabel=EXECUTION_MODEL_LABELS[p.model_tier]||modelInfo.label||p.model_tier;const hours=Math.round(researchSeconds/3600);$('#execution-model-description').textContent=(uiLanguage()==='en'?englishDescriptions[p.model_tier]:modelInfo.description)||(uiLanguage()==='en'?'No description is available for this tier.':'当前档位暂无模型说明。');$('#deployment-qwen-note').classList.toggle('hidden',p.model_tier!=='qwen_only');$('#deployment-hud-model').textContent=modelLabel.toUpperCase();$('#deployment-hud-duration').textContent=`${hours} ${researchSeconds===3600?'HOUR':'HOURS'}`;$('#deployment-hud-window').textContent=`≈ ${Math.round(researchSeconds/60)} MIN`;$('#execution-estimated-cost').textContent=compactCredit(estimate).replace(/ Credits$/,' CREDIT');$('#execution-pricing-version').textContent=uiLanguage()==='en'?'Estimate only':'非最终计费';
  $('#execution-time-estimate').textContent=uiLanguage()==='en'?`After the research window, allow about ${Math.round(publicationSeconds/60)} additional minutes for publication. Estimated total: ${formatRunDuration(totalSeconds)}.`:`研究窗口结束后，预计另需约 ${Math.round(publicationSeconds/60)} 分钟整理结果；当前预计总研究时间：${formatRunDuration(totalSeconds)}。`;updateExecutionSlogan(p.model_tier,p.duration_tier);
}
async function selectExecutionTier(kind,key){
  if(isConversationLocked())return;const container=$(kind==='model'?'#execution-model-tier':'#execution-duration-tier');if(container.dataset.value===key)return;container.dataset.value=key;syncDeploymentOptionStates(container);updateExecutionEstimate();await patchExecutionPolicy();
}
async function patchExecutionPolicy() {
  if (isConversationLocked()) return;
  try {
    const p=currentCatalogPolicy();
    const updated=await api(`/api/conversations/${state.conversation.conversation_id}/execution-policy`,{method:'PATCH',body:JSON.stringify({model_tier:p.model_tier,duration_tier:p.duration_tier})});
    state.conversation.execution_policy=updated;
    renderExecutionPolicy(updated);
  } catch(error){toast(error.message,true); await reloadConversation();}
}
async function renderContractChat() {
  const list=$('#contract-chat-list');const branch=selectedChatBranch();const sameBranch=Boolean(branch)&&state.chat.conversationId===state.conversation.conversation_id&&state.chat.contractVersionId===branch.contractVersionId;const nearBottom=!sameBranch||list.scrollHeight-list.scrollTop-list.clientHeight<72;const previousTop=sameBranch?list.scrollTop:0;const previousCount=sameBranch?state.chat.messageCount:0;
  if(!branch){closeContractChatStream();state.chat=freshChatState();list.replaceChildren();return;}
  const eventAfter=Number(state.conversation.contract_chat_event_sequences?.[branch.contractVersionId]||0);
  if(!sameBranch){closeContractChatStream();state.chat=freshChatState(state.conversation.conversation_id,branch.contractVersionId,branch.selectedVersion,0,eventAfter);}
  list.replaceChildren();
  const messages=(state.conversation.contract_chat_messages||[]).filter(item=>item.contract_version_id===branch.contractVersionId);
  if(!messages.length){list.innerHTML='<div class="empty-state"><p>这里是一段新的 Contract 对话。你可以询问设计理由，或明确要求修改 A–F。</p></div>';state.chat.messageCount=0;ensureContractChatStream();return;}
  for(const m of messages){const card=document.createElement('article');card.className=`chat-message ${m.role}`;const role=document.createElement('span');role.className='message-role';role.textContent=m.role;const body=document.createElement('div');body.className='markdown';card.append(role,body);list.append(card);await renderMarkdown(body,m.content);}
  if(state.chat.liveStreamId){state.chat.liveCard=null;const live=showLiveChatCard(state.chat.liveStreamId);await renderMarkdown(live.querySelector('.markdown'),state.chat.liveSource);}
  const hasNew=messages.length>previousCount;
  if(previousCount===0||nearBottom)list.scrollTop=list.scrollHeight;else{list.scrollTop=previousTop;$('#contract-chat-new').classList.toggle('hidden',!hasNew);}
  state.chat.messageCount=messages.length;
  if(!state.chat.busy)state.chat.eventAfter=Math.max(state.chat.eventAfter,eventAfter);
  ensureContractChatStream();
}
function setContractChatStatus(message=''){
  const status=$('#contract-chat-status');if(!status)return;status.textContent=message;status.classList.toggle('hidden',!message);
}
function markContractChatActivity(){
  if(state.chat.statusTimer)clearTimeout(state.chat.statusTimer);setContractChatStatus('正在生成回复…');
  state.chat.statusTimer=setTimeout(()=>{state.chat.statusTimer=null;if(state.chat.busy||state.chat.liveStreamId)setContractChatStatus('仍在生成回复或校验 Working Draft…');},4000);
}
function finishContractChatActivity(){if(state.chat.statusTimer)clearTimeout(state.chat.statusTimer);state.chat.statusTimer=null;setContractChatStatus();}
function closeContractChatStream(){if(state.chat?.eventSource)state.chat.eventSource.close();if(state.chat?.renderTimer)clearTimeout(state.chat.renderTimer);if(state.chat?.statusTimer)clearTimeout(state.chat.statusTimer);if(state.chat){state.chat.eventSource=null;state.chat.renderTimer=null;state.chat.statusTimer=null;}setContractChatStatus();}
function ensureContractChatStream(){
  const conversationId=state.conversation?.conversation_id;const selectedVersion=state.chat.selectedVersion;if(!conversationId||!selectedVersion||state.chat.eventSource)return;
  const source=new EventSource(`/api/conversations/${conversationId}/chat/events?selected_version=${selectedVersion}&after=${state.chat.eventAfter}`);state.chat.eventSource=source;
  source.addEventListener('contract_chat',event=>{let payload;try{payload=JSON.parse(event.data);}catch(error){console.warn('Contract Chat SSE event failed',error);return;}handleContractChatStreamEvent(payload).catch(error=>{console.warn('Contract Chat SSE event failed',error);toast('Contract 已保存，但页面自动同步失败；请重试或刷新。',true);});});
}
function makeChatCard(role,content='',live=false){
  const card=document.createElement('article');card.className=`chat-message ${role}${live?' streaming':''}`;const label=document.createElement('span');label.className='message-role';label.textContent=role;const body=document.createElement('div');body.className='markdown';card.append(label,body);$('#contract-chat-list .empty-state')?.remove();$('#contract-chat-list').append(card);renderMarkdown(body,content);return card;
}
function chatNearBottom(){const list=$('#contract-chat-list');return list.scrollHeight-list.scrollTop-list.clientHeight<72;}
function showLiveChatCard(streamId){
  if(state.chat.liveStreamId===streamId&&state.chat.liveCard)return state.chat.liveCard;
  if(state.chat.liveStreamId!==streamId){state.chat.liveStreamId=streamId;state.chat.liveSource='';}state.chat.liveCard=makeChatCard('assistant',state.chat.liveSource,true);return state.chat.liveCard;
}
function scheduleLiveChatRender(){
  if(state.chat.renderTimer)return;const follow=chatNearBottom();state.chat.renderTimer=setTimeout(async()=>{state.chat.renderTimer=null;const card=state.chat.liveCard;if(!card)return;await renderMarkdown(card.querySelector('.markdown'),state.chat.liveSource);if(follow){const list=$('#contract-chat-list');list.scrollTop=list.scrollHeight;}else $('#contract-chat-new').classList.remove('hidden');},90);
}
function rememberChatStream(set,streamId){if(!streamId)return;set.add(streamId);if(set.size>32)set.delete(set.values().next().value);}
async function finalizeContractChatStream(streamId,reply){
  if(!streamId||state.chat.finalizedStreams.has(streamId))return;
  const card=showLiveChatCard(streamId);if(state.chat.renderTimer){clearTimeout(state.chat.renderTimer);state.chat.renderTimer=null;}
  state.chat.liveSource=String(reply||state.chat.liveSource);await renderMarkdown(card.querySelector('.markdown'),state.chat.liveSource);card.classList.remove('streaming');
  state.chat.liveCard=null;state.chat.liveStreamId=null;state.chat.messageCount+=1;rememberChatStream(state.chat.finalizedStreams,streamId);
  if(chatNearBottom())$('#contract-chat-list').scrollTop=$('#contract-chat-list').scrollHeight;else $('#contract-chat-new').classList.remove('hidden');
}
async function syncContractChatCompletion(payload={}){
  const conversationId=state.conversation?.conversation_id;const streamId=String(payload.stream_id||'');
  if(!conversationId||conversationId!==state.chat.conversationId||state.chat.syncedStreams.has(streamId))return;
  if(payload.contract_version_id&&payload.contract_version_id!==state.chat.contractVersionId){
    const detail=await api(`/api/conversations/${conversationId}`);if(state.conversation?.conversation_id===conversationId){state.conversation=detail;renderNavigation();}return;
  }
  if(state.chat.syncPromise){await state.chat.syncPromise;if(state.chat.syncedStreams.has(streamId))return;}
  const draftChanged=Boolean(payload.draft_changed);const targetDraftRevision=Number(payload.draft_revision_after||0);const currentDraftRevision=Number(state.conversation.working_draft?.draft_revision||0);
  if(draftChanged&&targetDraftRevision>0&&currentDraftRevision>=targetDraftRevision){rememberChatStream(state.chat.syncedStreams,streamId);return;}
  const contractTop=$('#contract-main').scrollTop;rememberContractScroll();
  state.chat.syncPromise=(async()=>{
    const detail=await api(`/api/conversations/${conversationId}`);if(state.conversation?.conversation_id!==conversationId)return;
    state.conversation=detail;
    if(draftChanged&&detail.working_draft){
      if(state.selected.kind!=='draft')state.contractScroll.set(`${conversationId}:draft`,contractTop);
      setContractSelection({kind:'draft',version:null});
    }else chooseDefaultContractView();
    renderNavigation();
    if(draftChanged)await renderConversation();else await renderContractChat();
    rememberChatStream(state.chat.syncedStreams,streamId);
    return true;
  })().catch(error=>{console.warn('Contract Chat completion sync failed',error);toast('Contract 已保存，但页面自动同步失败；请重试或刷新。',true);return false;});
  try{await state.chat.syncPromise;}finally{state.chat.syncPromise=null;}
}
async function handleContractChatStreamEvent(event){
  const sequence=Number(event.sequence||0);if(sequence<=state.chat.eventAfter)return;state.chat.eventAfter=sequence;const kind=String(event.kind||'');const payload=event.payload||{};const streamId=String(payload.stream_id||'');
  if(state.chat.finalizedStreams.has(streamId)&&['chat_started','chat_delta','chat_reset'].includes(kind))return;
  if(kind==='chat_started'){showLiveChatCard(streamId);markContractChatActivity();scheduleLiveChatRender();return;}
  if(kind==='chat_delta'){showLiveChatCard(streamId);state.chat.liveSource+=String(payload.delta||'');markContractChatActivity();scheduleLiveChatRender();return;}
  if(kind==='chat_reset'){showLiveChatCard(streamId);state.chat.liveSource=String(payload.content||'');markContractChatActivity();scheduleLiveChatRender();return;}
  if(kind==='chat_completed'){
    await finalizeContractChatStream(streamId,payload.reply);finishContractChatActivity();await syncContractChatCompletion(payload);return;
  }
  if(kind==='chat_failed'){
    const card=showLiveChatCard(streamId);card.classList.remove('streaming');card.classList.add('failed');state.chat.liveSource=`Contract Chat 失败：${payload.error||'未知错误'}`;await renderMarkdown(card.querySelector('.markdown'),state.chat.liveSource);state.chat.liveCard=null;state.chat.liveStreamId=null;finishContractChatActivity();
  }
}
function startGenerationPollingIfNeeded(){
  const currentActive=state.conversation?.generation&&['queued','running','repairing'].includes(state.conversation.generation.status)?state.conversation.conversation_id:null;
  const background=(state.bootstrap?.conversations||[]).find(item=>item.status==='contract_generating')?.conversation_id;
  const target=currentActive||background;
  if(!target){stopGenerationPolling();return;}
  if(state.generationTimer&&state.generationConversationId===target)return;
  stopGenerationPolling();state.generationConversationId=target;state.generationTimer=setInterval(pollGeneration,1500);pollGeneration();
}
function stopGenerationPolling(){if(state.generationTimer)clearInterval(state.generationTimer);state.generationTimer=null;state.generationConversationId=null;}
async function pollGeneration(){const conversationId=state.generationConversationId;if(!conversationId)return;try{const status=await api(`/api/conversations/${conversationId}/generation`);const attempt=status.attempt;if(!attempt)return;if(state.conversation?.conversation_id===conversationId){state.conversation.generation=attempt;await renderGenerationPanel();await renderContract();}if(['completed','failed','interrupted'].includes(attempt.status)){stopGenerationPolling();await refreshBootstrap({keepConversation:true});if(state.conversation?.conversation_id===conversationId){if(attempt.status==='failed')setContractSelection({kind:'version',version:state.conversation.latest_contract_version||null});await reloadConversation();}if(attempt.status==='failed')showAlert('Research Contract 生成失败',attempt.error||'Contract generation failed');}}catch(error){console.warn(error);}}
async function resumeContractGeneration(event){
  const generation=state.conversation?.generation;if(!generation?.resumable)return;
  await withBusy(event.currentTarget,async()=>{try{
    await api(`/api/conversations/${state.conversation.conversation_id}/generation/resume`,{method:'POST',body:JSON.stringify({generation_attempt_id:generation.generation_attempt_id})});
    await reloadConversation();startGenerationPollingIfNeeded();toast('Contract generation 已从保存的 checkpoint 继续');
  }catch(error){showAlert('继续 Contract 执行失败',error.message);}});
}
async function reloadConversation(){if(!state.conversation)return;const id=state.conversation.conversation_id;state.conversation=await api(`/api/conversations/${id}`);chooseDefaultContractView();renderNavigation();await renderConversation();}

async function openSectionEditor(key,fragmentId=null) {
  if(isConversationLocked())return;
  try{
    const data=selectedContractData();if(!data)return;
    const requestedBase=data.kind==='draft'?data.base_version:data.version;
    const existing=state.conversation.working_draft;
    if(existing&&data.kind==='version'&&Number(existing.base_version)!==Number(requestedBase))throw new Error(`当前已有基于 Contract v${existing.base_version} 的 Working Draft；请先保存或放弃该草稿，再编辑 Contract v${requestedBase}。`);
    const wholeSource=String(data.sections?.[key]||'');const split=splitNodeFragments(wholeSource,key);const normalizedId=fragmentId===null?null:String(fragmentId).toUpperCase();const fragment=normalizedId===null?null:split.fragments.find(item=>item.id===normalizedId)||null;if(normalizedId&&!fragment)throw new Error(`Fragment ${normalizedId} 不存在或不再可编辑`);const editorSource=fragment?fragment.body:wholeSource;
    state.section.key=key;state.section.fragment=fragment?{...fragment}:null;state.section.wholeSource=wholeSource;state.section.baseVersion=requestedBase;state.section.openingContent=editorSource;state.section.saveQueued=false;state.section.refreshAfterClose=false;$('#section-editor').value=editorSource;
    $('#fragment-identity').classList.toggle('hidden',!fragment);$('#fragment-id').textContent=fragment?.id||'';$('#fragment-title').value=fragment?.title||'';
    state.section.lastSavedContent=sectionEditorSnapshot();
    const spec=state.bootstrap.application.contract_sections.find(s=>s.key===key);renderMathText($('#section-heading'),fragment?.displayTitle||spec?.heading||key);setSaveState('#section-save-state',data.kind==='draft'?'已暂存':'尚未修改；关闭不会创建 Working Draft');$('#section-validation').classList.add('hidden');openDialog('#section-dialog');scheduleSectionPreview(true);
  }catch(error){toast(error.message,true);}
}
function sectionEditorSnapshot(){return state.section.fragment?JSON.stringify([$('#fragment-title').value,$('#section-editor').value]):$('#section-editor').value;}
function sectionEditorPreviewSource(){if(!state.section.fragment)return $('#section-editor').value;const title=$('#fragment-title').value.trim();return `### ${state.section.fragment.id}：${title}\n\n${$('#section-editor').value}`;}
function scheduleSectionPreview(immediate=false){clearTimeout(state.section.previewTimer);state.section.previewTimer=setTimeout(()=>{renderMarkdown($('#section-preview'),sectionEditorPreviewSource());restoreEditorScrollRatio();},immediate?0:200);}
function scheduleSectionAutosave(){clearTimeout(state.section.saveTimer);setSaveState('#section-save-state','等待自动保存…','saving');state.section.saveTimer=setTimeout(()=>saveCurrentSection(),800);}
async function saveCurrentSection({silent=true}={}){
  if(!state.section.key||!state.conversation)return null;
  clearTimeout(state.section.saveTimer);
  state.section.saveQueued=true;
  if(state.section.savePromise)return state.section.savePromise;
  state.section.savePromise=(async()=>{
    let last=null;
    while(state.section.saveQueued&&state.section.key&&state.conversation){
      state.section.saveQueued=false;
      const key=state.section.key;
      const editorContent=$('#section-editor').value;
      const editorSnapshot=sectionEditorSnapshot();
      if(state.section.lastSavedContent===editorSnapshot){
        const revision=state.conversation.working_draft?.draft_revision;setSaveState('#section-save-state',revision?'已暂存':'尚未修改；关闭不会创建 Working Draft');
        continue;
      }
      const draft=state.conversation.working_draft;
      const fragment=state.section.fragment;
      const fragmentTitle=fragment?$('#fragment-title').value.trim():'';
      if(fragment&&!fragmentTitle){setSaveState('#section-save-state','标题不能为空','error');$('#section-validation').textContent='Node/Direction 的稳定 ID 不能修改，自然语言标题不能为空。';$('#section-validation').classList.remove('hidden');return null;}
      setSaveState('#section-save-state','保存中…','saving');
      try{
        const path=fragment?`/api/conversations/${state.conversation.conversation_id}/draft/fragment`:`/api/conversations/${state.conversation.conversation_id}/draft`;
        const payload=fragment?{section_key:key,fragment_id:fragment.id,title:fragmentTitle,content:editorContent,expected_revision:draft?.draft_revision||0,base_version:state.section.baseVersion}:{section_key:key,content:editorContent,expected_revision:draft?.draft_revision||0,base_version:state.section.baseVersion};
        const updated=await api(path,{method:'PATCH',body:JSON.stringify(payload)});
        state.conversation.working_draft=updated;
        setContractSelection({kind:'draft',version:null});
        state.section.lastSavedContent=editorSnapshot;
        state.section.refreshAfterClose=true;
        if(fragment){state.section.wholeSource=String(updated.sections?.[key]||'');const saved=splitNodeFragments(state.section.wholeSource,key).fragments.find(item=>item.id===fragment.id);if(!saved)throw new Error(`保存后未找到 Fragment ${fragment.id}`);state.section.fragment={...saved};}else state.section.wholeSource=editorContent;
        $('#section-validation').classList.add('hidden');
        last=updated;
        setSaveState('#section-save-state','已暂存');
        if(!silent)toast('Working Draft 已暂存');
      }catch(error){
        setSaveState('#section-save-state',error.message,'error');
        $('#section-validation').textContent=error.message;
        $('#section-validation').classList.remove('hidden');
        if(!silent)toast(error.message,true);
        if(error.status===409)await reloadConversation();
        return null;
      }
    }
    return last||state.conversation?.working_draft||null;
  })().finally(()=>{state.section.savePromise=null;});
  return state.section.savePromise;
}
function editorScrollRatio(el){const max=el.scrollHeight-el.clientHeight;return max>0?el.scrollTop/max:0;}function setEditorRatio(el,ratio){const max=el.scrollHeight-el.clientHeight;el.scrollTop=max*ratio;}function restoreEditorScrollRatio(){const ratio=editorScrollRatio($('#section-editor'));requestAnimationFrame(()=>setEditorRatio($('#section-preview'),ratio));}
function setupLinkedScroll(left,right){
  let frame=null;const expected=new WeakMap();
  const handle=(from,to)=>{
    const programmed=expected.get(from);
    if(programmed!==undefined&&Math.abs(from.scrollTop-programmed)<1){expected.delete(from);return;}
    expected.delete(from);if(frame)cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>{frame=null;const max=to.scrollHeight-to.clientHeight;const target=max*Math.max(0,Math.min(1,editorScrollRatio(from)));if(Math.abs(to.scrollTop-target)<1)return;expected.set(to,target);to.scrollTop=target;});
  };
  left.addEventListener('scroll',()=>handle(left,right),{passive:true});right.addEventListener('scroll',()=>handle(right,left),{passive:true});
  for(const element of [left,right])element.addEventListener('wheel',()=>{if(frame)cancelAnimationFrame(frame);frame=null;expected.delete(element);},{passive:true});
}
function setupScrollSync(){setupLinkedScroll($('#section-editor'),$('#section-preview'));}
function setupRequestScrollSync(){setupLinkedScroll($('#request-editor-source'),$('#request-editor-preview'));}
function setupDragDivider(divider,onRatio){let active=false;let container=null;const move=e=>{if(!active)return;const r=container.getBoundingClientRect();const ratio=Math.min(.75,Math.max(.25,(e.clientX-r.left)/r.width));onRatio(ratio);};divider.addEventListener('pointerdown',e=>{active=true;container=divider.parentElement;divider.classList.add('dragging');divider.setPointerCapture(e.pointerId)});divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',e=>{active=false;divider.classList.remove('dragging');divider.releasePointerCapture(e.pointerId)});}

async function commitDraft(){try{await flushSectionIfOpen();const result=await api(`/api/conversations/${state.conversation.conversation_id}/draft/commit`,{method:'POST'});toast(`Contract v${result.version} 已保存`);setContractSelection({kind:'version',version:result.version});await refreshBootstrap({keepConversation:true});await reloadConversation();}catch(error){toast(error.message,true);}}
async function discardDraft(){confirmAction('放弃 Working Draft','这会永久丢弃尚未冻结的 Draft；历史 Contract Version 不受影响。',async()=>{try{await api(`/api/conversations/${state.conversation.conversation_id}/draft`,{method:'DELETE'});setContractSelection({kind:'version',version:state.conversation.latest_contract_version});await reloadConversation();toast('Working Draft 已放弃');}catch(error){toast(error.message,true);}},'放弃草稿');}
async function flushSectionIfOpen(){if($('#section-dialog').open&&state.section.key)await saveCurrentSection({silent:true});}
async function finishSectionEditor(){const dirty=state.section.lastSavedContent!==sectionEditorSnapshot();const saved=await saveCurrentSection({silent:true});if(dirty&&!saved)return;closeDialog('#section-dialog');const refresh=state.section.refreshAfterClose;state.section.key=null;state.section.fragment=null;$('#fragment-title').value='';$('#fragment-identity').classList.add('hidden');if(refresh)await renderConversation();}
async function approveContract(ack=false){try{await flushSectionIfOpen();const target=selectedContractData();if(!target)return;const research_language=$('#execution-research-language').value||state.bootstrap.current_user.default_research_language||'zh';const payload=target.kind==='draft'?{target_kind:'working_draft',expected_draft_revision:target.draft_revision,research_language}:{target_kind:'immutable_version',version:target.version,acknowledge_historical_with_draft:ack,research_language};const result=await api(`/api/conversations/${state.conversation.conversation_id}/approve`,{method:'POST',body:JSON.stringify(payload)});toast(uiLanguage()==='en'?'Research Run created':'Research Run 已创建');await refreshBootstrap({keepConversation:true});await openRun(result.run_id,true);}catch(error){if(String(error.message).startsWith('WorkingDraftApprovalWarning:')){confirmAction('批准历史 Contract Version',error.message.replace('WorkingDraftApprovalWarning:',''),()=>approveContract(true),'确认执行历史版本');}else toast(error.message,true);}}
async function cloneContract(){const d=selectedContractData();if(!d||d.kind!=='version'){toast('请先选择一个 immutable Contract Version',true);return;}try{const result=await api(`/api/conversations/${state.conversation.conversation_id}/clone`,{method:'POST',body:JSON.stringify({version:d.version})});await refreshBootstrap({keepConversation:true});await openConversation(result.conversation_id);if(result.missing_sources?.length)toast(`分支已创建；缺失源文件：${result.missing_sources.join('、')}`,true);else toast('已从当前 Contract 新建独立对话');}catch(error){toast(error.message,true);}}
async function branchFromCurrentSelection(){if(state.selected.kind==='request'){prefillNewRequestFromCurrent();return;}await cloneContract();}
function editRegenerationInstruction(){const editor=$('#regenerate-instruction');$('#regenerate-instruction-preview').classList.add('hidden');$('#regenerate-instruction-hint').classList.add('hidden');editor.classList.remove('hidden');requestAnimationFrame(()=>editor.focus({preventScroll:true}));}
async function previewRegenerationInstruction(){const editor=$('#regenerate-instruction');const source=editor.value.trim();if(!source){editRegenerationInstruction();return;}const preview=$('#regenerate-instruction-preview');await renderMarkdown(preview,source);editor.classList.add('hidden');preview.classList.remove('hidden');$('#regenerate-instruction-hint').classList.remove('hidden');}
async function fullRegenerate(){if(isConversationLocked())return;if(activeContractGeneration()){toast('正在重新规划中，该功能暂不可用',true);return;}const d=selectedContractData();state.regenerationVersion=d?.kind==='version'?d.version:state.conversation.working_draft?.base_version||state.conversation.latest_contract_version;const baseLabel=d?.kind==='draft'?`Working Draft（基于 Contract v${state.regenerationVersion}）`:`Contract v${state.regenerationVersion}`;$('#regenerate-base-version').textContent=`当前修改基于版本：${baseLabel}`;$('#regenerate-instruction').value='';editRegenerationInstruction();openDialog('#regenerate-dialog');requestAnimationFrame(()=>$('#regenerate-instruction').focus({preventScroll:true}));}
async function submitFullRegeneration(event){event.preventDefault();if(activeContractGeneration()){closeDialog('#regenerate-dialog');toast('正在重新规划中，该功能暂不可用',true);return;}const version=state.regenerationVersion;if(!version)return;await withBusy(event.submitter||$('#regenerate-accept'),async()=>{try{await api(`/api/conversations/${state.conversation.conversation_id}/regenerate`,{method:'POST',body:JSON.stringify({selected_version:version,user_instruction:$('#regenerate-instruction').value.trim(),idempotency_key:uid('contract-regenerate')})});closeDialog('#regenerate-dialog');state.regenerationVersion=null;setContractSelection({kind:'pending',version:Number(state.conversation.latest_contract_version||0)+1});state.generationNoticeClosed=false;$('#generation-notice').classList.remove('hidden');await reloadConversation();toast('完整重新生成已进入后台队列');}catch(error){showAlert('重新生成失败',error.message);}});}
async function sendContractChat(event){event.preventDefault();const input=$('#contract-chat-input');const message=input.value.trim();if(!message||state.chat.busy)return;const branch=selectedChatBranch();if(!branch)return;ensureContractChatStream();input.value='';makeChatCard('user',message);const list=$('#contract-chat-list');list.scrollTop=list.scrollHeight;state.chat.busy=true;markContractChatActivity();await withBusy(event.submitter||$('#contract-chat-form button[type="submit"]'),async()=>{try{const draft=state.conversation.working_draft;const selectedVersion=branch.selectedVersion;const compatible=!draft||Number(draft.base_version)===Number(selectedVersion);const result=await api(`/api/conversations/${state.conversation.conversation_id}/chat`,{method:'POST',body:JSON.stringify({message,allow_draft_changes:compatible,expected_draft_revision:compatible?(draft?.draft_revision??0):0,selected_version:selectedVersion,profile:state.conversation.contractor_profile||'high'})});const changed=Boolean(result.draft_changed??Object.keys(result.patches||{}).length);if(result.contract_version_id===state.chat.contractVersionId)await finalizeContractChatStream(String(result.stream_id||''),result.reply);await syncContractChatCompletion({...result,draft_changed:changed});if(!compatible&&!changed)toast('当前 Working Draft 基于另一历史版本；本轮只进行了讨论，没有修改草稿。');else toast(changed?'Contract Chat 已更新 Working Draft':'Contract Chat 已回复');}catch(error){finishContractChatActivity();showAlert('Contract Chat 失败',error.message);if(error.status===409)await reloadConversation();}finally{state.chat.busy=false;finishContractChatActivity();}});}

async function syncRunIntoBootstrap(run,newEvents=[]) {
  if(!state.bootstrap||!run)return;
  const conversation=(state.bootstrap.conversations||[]).find(item=>item.conversation_id===run.conversation_id);
  if(conversation){conversation.run_id=run.run_id;conversation.run_status=run.status;conversation.runtime_phase=run.runtime_phase;const row=document.querySelector(`.chat-row[data-conversation-id="${CSS.escape(run.conversation_id)}"]`);const meta=row?.querySelector('small');if(meta)meta.textContent=conversationMeta(conversation);}
  const recent=(state.bootstrap.recent_research||[]).find(item=>item.conversation_id===run.conversation_id);
  if(recent){recent.run_id=run.run_id;recent.run_status=run.status;recent.runtime_phase=run.runtime_phase;const latest=newEvents.at(-1);if(latest){recent.latest_progress={title:latest.title,summary:latest.summary,created_at:latest.created_at};recent.updated_at=latest.created_at;}const card=document.querySelector(`#recent-research-list [data-conversation-id="${CSS.escape(run.conversation_id)}"]`);if(card){const status=recentResearchStatus(recent);const badge=card.querySelector('.recent-research-status');if(badge){badge.className=`recent-research-status ${status.tone}`;badge.textContent=status.label;}const progress=card.querySelector('.recent-research-progress');if(progress)await renderMathTextIfChanged(progress,recent.latest_progress?.summary||recentResearchFallback(recent));}}
  if(state.conversation?.conversation_id===run.conversation_id&&state.conversation.run)Object.assign(state.conversation.run,{run_id:run.run_id,status:run.status,runtime_phase:run.runtime_phase});
}
async function openRun(runId,switchView=true){stopRunPolling();stopRunLoadingQuotes();try{state.run=await api(`/api/runs/${runId}`);state.runEvents=[];state.runEventAfter=0;state.runUi=freshRunUiState();$('#event-list').replaceChildren();$('#claim-list').replaceChildren();if(switchView){state.viewingRunContract=false;showView('run');}await syncRunIntoBootstrap(state.run);await renderRun();await pollRun();startRunPolling();}catch(error){toast(error.message,true);}}
function formatCompactNumber(value){const n=Number(value||0);if(n>=1e6)return `${(n/1e6).toFixed(n>=1e7?1:2).replace(/\.0+$/,'')}M`;if(n>=1e3)return `${(n/1e3).toFixed(n>=1e5?0:1).replace(/\.0$/,'')}k`;return String(Math.round(n));}
function formatRunDuration(seconds){const total=Math.max(0,Number(seconds||0));const hours=Math.floor(total/3600);const minutes=Math.floor(total%3600/60);if(uiLanguage()==='en')return hours?`${hours}h ${minutes}m`:`${minutes}m`;return hours?`${hours}小时${minutes}分钟`:`${minutes}分钟`;}
function runPhaseLabel(phase){const labels={waiting:t('runWaiting'),research:t('runResearch'),draining:t('runDraining'),paused:t('runPaused'),resuming:t('runResuming'),paper_writing:t('runWriting'),paper_audit:t('runAudit'),paper_revision:t('runRevision'),paper_published:t('runPublishing'),completed:t('runCompleted'),failed:t('runFailed'),interrupted:t('runInterrupted')};return labels[phase]||phase||t('runResearch');}
function renderRunProgress(){const run=state.run;const budget=Number(run.research_budget_seconds||0);const elapsed=Number(run.research_elapsed_seconds||0);const publicationEstimate=Number(run.publication_estimate_seconds||2700);const totalEstimate=budget+publicationEstimate;const researchShare=totalEstimate>0?budget/totalEstimate*100:0;const paperMilestones={paper_writing:0,paper_audit:1,paper_revision:2,paper_published:3};const progressPhase=run.progress_phase||run.runtime_phase;let percent=totalEstimate>0?Math.min(researchShare,Math.max(0,elapsed/totalEstimate*100)):0;let copy=run.started_at?(uiLanguage()==='en'?`Research elapsed: ${formatRunDuration(elapsed)}`:`已进行研究 ${formatRunDuration(elapsed)}`):(uiLanguage()==='en'?'Research has not started':'尚未开始研究');if(Object.hasOwn(paperMilestones,progressPhase)){const writingElapsed=Number(run.publication_elapsed_seconds||0);const remaining=100-researchShare;percent=researchShare+remaining*paperMilestones[progressPhase]/3;copy=uiLanguage()==='en'?`Publication elapsed: ${formatRunDuration(writingElapsed)}`:`报告整理已进行 ${formatRunDuration(writingElapsed)}`;}if(run.runtime_phase==='completed')percent=100;percent=Math.min(100,Math.max(0,percent));$('#run-phase-label').textContent=runPhaseLabel(run.runtime_phase);$('#run-budget-copy').textContent=copy;const bar=$('#run-progress-bar');const track=bar.parentElement;bar.style.width=`${percent}%`;track.setAttribute('aria-valuenow',String(Math.round(percent)));track.classList.toggle('terminal-error',['failed','interrupted'].includes(run.runtime_phase));track.classList.toggle('paused',run.runtime_phase==='paused'||run.runtime_phase==='resuming');const usage=run.usage||{};$('#run-input-tokens').textContent=formatCompactNumber(usage.input_tokens);$('#run-cached-tokens').textContent=formatCompactNumber(usage.cached_input_tokens);$('#run-output-tokens').textContent=formatCompactNumber(usage.output_tokens);}
function runGraphFingerprint(nodes){return JSON.stringify((nodes||[]).map(n=>[n.node_id,n.kind,n.status,n.display_title||n.title,n.display_summary||n.objective,n.display_status]));}
function stopRunLoadingQuotes(){const ui=state.runUi;if(ui?.loadingQuoteTimer)clearInterval(ui.loadingQuoteTimer);if(ui?.loadingQuoteFadeTimer)clearTimeout(ui.loadingQuoteFadeTimer);if(ui){ui.loadingQuoteTimer=null;ui.loadingQuoteFadeTimer=null;ui.graphFingerprint='';}$('#node-grid')?.classList.remove('research-loading-active');}
function nextRunLoadingQuote({immediate=false}={}){const ui=state.runUi;const quote=$('#run-loading-quote');if(!quote||!ui)return;const quotes=uiLanguage()==='en'?RESEARCH_LOADING_QUOTES_EN:RESEARCH_LOADING_QUOTES;let next=Math.floor(Math.random()*quotes.length);if(next===ui.loadingQuoteIndex)next=(next+1)%quotes.length;const apply=()=>{quote.textContent=quotes[next];ui.loadingQuoteIndex=next;quote.classList.remove('is-changing');ui.loadingQuoteFadeTimer=null;};if(immediate){apply();return;}quote.classList.add('is-changing');if(ui.loadingQuoteFadeTimer)clearTimeout(ui.loadingQuoteFadeTimer);ui.loadingQuoteFadeTimer=setTimeout(apply,140);}
function startRunLoadingQuotes(){if(state.runUi.loadingQuoteTimer)return;nextRunLoadingQuote({immediate:true});state.runUi.loadingQuoteTimer=setInterval(()=>nextRunLoadingQuote(),6000);}
function shouldShowRunLoading(){return !(state.run?.nodes||[]).length&&['waiting','research','resuming'].includes(state.run?.runtime_phase)&&!['failed','interrupted','completed','paused'].includes(state.run?.status);}
function captureRunGraphViewport(grid){const rect=grid.getBoundingClientRect();const visible=[...grid.querySelectorAll('[data-node-id]')].map(card=>({card,rect:card.getBoundingClientRect()})).find(item=>item.rect.bottom>rect.top&&item.rect.top<rect.bottom);return {scrollTop:grid.scrollTop,nodeId:visible?.card.dataset.nodeId||null,offset:visible?visible.rect.top-rect.top:null,interactionVersion:state.runUi.graphInteractionVersion};}
function restoreRunGraphViewport(grid,snapshot){if(!snapshot||snapshot.interactionVersion!==state.runUi.graphInteractionVersion)return;const card=snapshot.nodeId?grid.querySelector(`[data-node-id="${CSS.escape(snapshot.nodeId)}"]`):null;if(card&&snapshot.offset!==null){const current=card.getBoundingClientRect().top-grid.getBoundingClientRect().top;grid.scrollTop+=current-snapshot.offset;}else grid.scrollTop=snapshot.scrollTop;}
async function renderRunGraph(){
  const grid=$('#node-grid');const nodes=state.run.nodes||[];const loading=shouldShowRunLoading();const fingerprint=nodes.length?runGraphFingerprint(nodes):JSON.stringify(['empty',state.run.runtime_phase,state.run.status,loading]);if(fingerprint===state.runUi.graphFingerprint)return;
  const viewport=captureRunGraphViewport(grid);const existing=new Map([...grid.querySelectorAll('[data-node-id]')].map(card=>[card.dataset.nodeId,card]));const wanted=new Set();const mathEntries=[];const markdownEntries=[];
  stopRunLoadingQuotes();grid.querySelector('.muted')?.remove();grid.querySelector('.research-loading')?.remove();
  for(const n of nodes){wanted.add(n.node_id);let card=existing.get(n.node_id);if(!card){card=document.createElement('article');card.dataset.nodeId=n.node_id;const tag=document.createElement('span');tag.className='kind-tag';const h=document.createElement('h3');const p=document.createElement('div');p.className='node-summary markdown';card.append(tag,h,p);}const title=n.display_title||n.title;const summary=n.details?.public_card?.fallback?n.objective:(n.display_summary||n.objective);const signature=JSON.stringify([n.kind,n.status,title,summary,n.display_status]);if(card.dataset.nodeSignature!==signature){card.className=`node-card ${n.kind}`;card.querySelector('.kind-tag').textContent=`${n.kind} · ${n.status}`;mathEntries.push({container:card.querySelector('h3'),source:title});markdownEntries.push({container:card.querySelector('.node-summary'),source:summary});card.dataset.nodeSignature=signature;}grid.append(card);}
  for(const [nodeId,card] of existing)if(!wanted.has(nodeId))card.remove();
  if(!nodes.length&&loading){grid.classList.add('research-loading-active');grid.innerHTML='<section class="research-loading"><div class="research-loading-inner"><p class="research-loading-status">PREPARING RESEARCH NODES</p><blockquote id="run-loading-quote" class="research-loading-quote"></blockquote></div></section>';startRunLoadingQuotes();}
  else if(!nodes.length)grid.innerHTML='<p class="muted">Research Nodes 尚未物化。</p>';
  await Promise.all([renderMathTextBatch(mathEntries),renderMarkdownBatch(markdownEntries)]);state.runUi.graphFingerprint=fingerprint;restoreRunGraphViewport(grid,viewport);
}
function eventListNearBottom(){const list=$('#event-list');return list.scrollHeight-list.scrollTop-list.clientHeight<=48;}
function updateEventUnread(){const button=$('#event-new-count');button.textContent=uiLanguage()==='en'?`↓ ${state.runUi.eventUnread} new`:`↓ ${state.runUi.eventUnread} 条新进展`;button.classList.toggle('hidden',state.runUi.eventUnread<1);}
async function renderRunEvents(){const list=$('#event-list');const follow=eventListNearBottom();const incoming=state.runEvents.filter(item=>!state.runUi.eventSequences.has(item.sequence));if(!incoming.length){if(!state.runEvents.length&&!list.children.length)list.innerHTML=`<p class="muted">${t('noEvents')}</p>`;return;}if(list.querySelector('.muted'))list.replaceChildren();const markdownEntries=[];for(const e of incoming){state.runUi.eventSequences.add(e.sequence);const row=document.createElement('article');row.className='event-item';row.dataset.eventSequence=e.sequence;const title=document.createElement('span');title.textContent=e.title;const time=document.createElement('small');time.textContent=new Date(e.created_at).toLocaleTimeString(uiLanguage()==='en'?'en-US':'zh-CN');const body=document.createElement('div');body.className='markdown';row.append(title,time,body);list.append(row);markdownEntries.push({container:body,source:e.summary});}await renderMarkdownBatch(markdownEntries);if(follow){list.scrollTop=list.scrollHeight;state.runUi.eventUnread=0;}else state.runUi.eventUnread+=incoming.length;updateEventUnread();}
function updateClaimUnread(){const button=$('#claim-new-count');button.textContent=uiLanguage()==='en'?`+${state.runUi.claimUnread} new`:`+${state.runUi.claimUnread} 新结果`;button.classList.toggle('hidden',state.runUi.claimUnread<1);}
async function renderVerifiedClaims(){const list=$('#claim-list');const claims=state.run.verified_claims||[];const incoming=claims.filter(item=>!state.runUi.claimIds.has(item.verified_claim_id));if(!claims.length){if(!list.children.length)list.innerHTML=`<p class="muted">${t('noClaims')}</p>`;state.runUi.claimsInitialized=true;return;}if(list.querySelector('.muted'))list.replaceChildren();const countAsNew=state.runUi.claimsInitialized;for(const c of incoming){state.runUi.claimIds.add(c.verified_claim_id);const card=document.createElement('article');card.className=`claim-item${countAsNew?' new-result':''}`;card.dataset.claimId=c.verified_claim_id;card.tabIndex=0;card.setAttribute('role','button');const heading=document.createElement('div');heading.className='claim-heading';heading.innerHTML=`<span>${t('newResult')}</span><span>${escapeHtml(c.verdict)}</span>`;const preview=document.createElement('div');preview.className='claim-preview markdown';card.append(heading,preview);list.append(card);}const markdownEntries=[];for(const c of claims){const card=list.querySelector(`[data-claim-id="${CSS.escape(c.verified_claim_id)}"]`);if(card){const preview=card.querySelector('.claim-preview');const summary=c.public_summary||(uiLanguage()==='en'?'Preparing a concise explanation…':'正在整理该结果的简要说明…');if(preview.dataset.markdownSource!==summary){markdownEntries.push({container:preview,source:summary});preview.dataset.markdownSource=summary;}}}await renderMarkdownBatch(markdownEntries);if(countAsNew)state.runUi.claimUnread+=incoming.length;state.runUi.claimsInitialized=true;updateClaimUnread();}
function showVerifiedClaimView(view){const proof=view==='proof';state.runUi.verifiedClaimView=proof?'proof':'statement';$('#verified-dialog-statement').classList.toggle('hidden',proof);$('#verified-dialog-proof').classList.toggle('hidden',!proof);const button=$('#verified-dialog-proof-toggle');button.textContent=proof?t('backStatement'):t('proof');const body=$('#verified-math-dialog .verified-dialog-body');body.scrollTop=0;}
async function openVerifiedClaim(claimId){const claim=(state.run?.verified_claims||[]).find(item=>item.verified_claim_id===claimId);if(!claim)return;state.runUi.activeClaimId=claimId;showVerifiedClaimView('statement');const button=$('#verified-dialog-proof-toggle');button.disabled=!claim.proof_available;button.textContent=claim.proof_available?t('proof'):t('proofUnavailable');await renderMathText($('#verified-dialog-title'),claim.public_summary||t('newResult'));$('#verified-dialog-verdict').textContent=claim.verdict;await renderMarkdown($('#verified-dialog-statement'),claim.statement);openDialog('#verified-math-dialog');$('#verified-math-dialog .verified-dialog-body').scrollTop=0;}
async function toggleVerifiedClaimProof(){const claimId=state.runUi.activeClaimId;if(!claimId)return;if(state.runUi.verifiedClaimView==='proof'){showVerifiedClaimView('statement');return;}const claim=(state.run?.verified_claims||[]).find(item=>item.verified_claim_id===claimId);if(!claim?.proof_available)return;const button=$('#verified-dialog-proof-toggle');button.disabled=true;button.textContent=t('loadingProof');try{let proof=state.runUi.claimProofs.get(claimId);if(!proof){proof=await api(`/api/runs/${state.run.run_id}/verified-claims/${claimId}/proof`);state.runUi.claimProofs.set(claimId,proof);}if(state.runUi.activeClaimId!==claimId)return;const container=$('#verified-dialog-proof');if(container.dataset.claimId!==claimId){await renderMarkdown(container,proof.proof_markdown);container.dataset.claimId=claimId;}showVerifiedClaimView('proof');}catch(error){toast(`${uiLanguage()==='en'?'Unable to load proof':'无法读取完整证明'}：${error.message}`,true);}finally{if(state.runUi.activeClaimId===claimId){button.disabled=false;if(state.runUi.verifiedClaimView!=='proof')button.textContent=t('proof');}}}
async function renderRun(){if(!state.run||state.activeView!=='run')return;await renderMathTextBatch([{container:$('#breadcrumb'),source:'Research Run'},{container:$('#page-title'),source:state.conversation?.display_name||state.run.run_id}].filter(entry=>entry.container.dataset.mathSource!==String(entry.source||'')));$('#breadcrumb').dataset.mathSource='Research Run';$('#page-title').dataset.mathSource=String(state.conversation?.display_name||state.run.run_id);$('#view-locked-contract').textContent=`${t('viewContract')} v${state.run.contract_version}`;$('#run-status').textContent=runPhaseLabel(state.run.runtime_phase);$('#run-graph-version').textContent=`graph v${state.run.graph_version}`;$('#graceful-drain-run').classList.toggle('hidden',!state.run.can_graceful_drain);const pauseButton=$('#pause-resume-run');const showPause=state.run.can_pause||state.run.can_resume||state.run.pause_pending||state.run.status==='resuming';pauseButton.classList.toggle('hidden',!showPause);pauseButton.disabled=Boolean(state.run.pause_pending||state.run.status==='resuming');pauseButton.textContent=state.run.pause_pending?(uiLanguage()==='en'?'Pausing all research calls…':'正在暂停所有研究调用'):(state.run.status==='resuming'?t('resuming'):(state.run.can_resume?t('resume'):t('pause')));const hasPdf=Boolean(state.run.final_paper_pdf_path);const hasTex=Boolean(state.run.final_paper_tex_path);const hasLegacy=Boolean(!hasPdf&&state.run.final_report_path);$('#developer-runtime').classList.toggle('hidden',!state.bootstrap?.current_user?.is_developer);$('#preview-paper').classList.toggle('hidden',!hasPdf);$('#download-paper').classList.toggle('hidden',!hasPdf);$('#download-paper-source').classList.toggle('hidden',!hasTex);$('#download-legacy-paper').classList.toggle('hidden',!hasLegacy);renderRunProgress();await renderRunGraph();await renderRunEvents();await renderVerifiedClaims();}

function requestGracefulDrain(){if(!state.run?.can_graceful_drain)return;confirmAction('直接收束当前研究','研究将停止开启新的方向，当前正在进行的工作会完成最后整理，随后进入论文写作。开始收束后无法恢复到研究阶段。',async()=>{try{await api(`/api/runs/${state.run.run_id}/graceful-drain`,{method:'POST'});toast('已请求收束当前研究');await pollRun();}catch(error){showAlert('无法收束当前研究',error.message);}},'确认收束并写报告');}
function pauseOrResumeRun(){if(!state.run)return;if(state.run.can_resume){api(`/api/runs/${state.run.run_id}/resume`,{method:'POST'}).then(()=>{toast('已请求继续运行');return pollRun();}).catch(error=>showAlert('无法继续运行',error.message));return;}if(!state.run.can_pause)return;confirmAction('暂停当前研究','研究会暂停在当前进度，正在进行的工作将停止，已有成果和研究状态都会保留。之后可以从这里继续。',async()=>{try{await api(`/api/runs/${state.run.run_id}/pause`,{method:'POST'});toast('正在暂停全部研究调用');await pollRun();}catch(error){showAlert('无法暂停运行',error.message);}},'确认暂停');}

async function openDeveloperRuntime(){if(!state.run)return;try{const data=await api(`/api/developer/runs/${state.run.run_id}/runtime`);$('#runtime-summary').textContent=`${data.files.length} files · expires ${data.runtime_expires_at||'not scheduled'} · cleaned ${data.runtime_cleaned_at||'no'}`;$('#runtime-keep-toggle').textContent=data.keep_runtime?'解除永久保留':'永久保留 Runtime';$('#runtime-keep-toggle').dataset.keep=String(data.keep_runtime);$('#runtime-file-list').innerHTML=data.files.map(file=>`<a class="admin-row" href="/api/developer/runs/${state.run.run_id}/runtime/file?path=${encodeURIComponent(file.path)}" target="_blank" rel="noopener"><div><strong>${escapeHtml(file.path)}</strong><small>${formatBytes(file.bytes)}</small></div></a>`).join('')||'<p class="muted">Runtime 已清理或没有文件。</p>';openDialog('#developer-runtime-dialog');}catch(error){toast(error.message,true);}}

async function previewFinalPaper(){
  const response=await fetch(`/api/runs/${state.run.run_id}/final-paper`,{credentials:'same-origin'});
  if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail||`${response.status}`);
  if(state.paperPreviewUrl)URL.revokeObjectURL(state.paperPreviewUrl);
  state.paperPreviewUrl=URL.createObjectURL(await response.blob());
  $('#paper-preview-frame').src=state.paperPreviewUrl;
  openDialog('#paper-preview-dialog');
}
async function viewLockedRunContract(){if(!state.run)return;stopRunPolling();try{if(state.conversation?.conversation_id!==state.run.conversation_id){rememberContractSelection();state.conversation=await api(`/api/conversations/${state.run.conversation_id}`);}setContractSelection({kind:'version',version:Number(state.run.contract_version)});state.viewingRunContract=true;renderNavigation();await renderConversation();}catch(error){toast(error.message,true);}}
async function returnToRun(){if(!state.run)return;state.viewingRunContract=false;showView('run');await pollRun();startRunPolling();}
function startRunPolling(){stopRunPolling();if(!state.run||state.activeView!=='run'||['completed','failed','interrupted'].includes(state.run.status))return;const epoch=state.runPollEpoch;const tick=async()=>{if(epoch!==state.runPollEpoch)return;state.runTimer=null;await pollRun();if(epoch===state.runPollEpoch&&state.run&&state.activeView==='run'&&!['completed','failed','interrupted'].includes(state.run.status))state.runTimer=setTimeout(tick,1800);};state.runTimer=setTimeout(tick,1800);}
function stopRunPolling(){state.runPollEpoch+=1;if(state.runTimer)clearTimeout(state.runTimer);state.runTimer=null;}
async function pollRun(){if(!state.run||state.runPollInFlight)return;state.runPollInFlight=true;try{const runId=state.run.run_id;const [feed,run]=await Promise.all([api(`/api/runs/${runId}/public-events?after=${state.runEventAfter}`),api(`/api/runs/${runId}`)]);if(state.run?.run_id!==runId)return;const incoming=feed.events||[];for(const e of incoming)state.runEvents.push(e);state.runEventAfter=Math.max(state.runEventAfter,Number(feed.next_after||0));state.run=run;await syncRunIntoBootstrap(run,incoming);if(state.activeView==='run')await renderRun();if(['completed','failed','interrupted'].includes(state.run.status))stopRunPolling();}catch(error){console.warn(error);}finally{state.runPollInFlight=false;}}

function renderFileLibrary(){const box=$('#file-list');if(!box||!state.bootstrap)return;box.innerHTML=(state.bootstrap.files||[]).map(f=>`<div class="file-row"><div><strong>${escapeHtml(f.name)}</strong><small>${formatBytes(f.size_bytes)} · ${escapeHtml(f.sha256.slice(0,12))}</small></div><div class="file-actions"><button class="ghost compact-button" type="button" data-download-file="${f.file_id}" data-file-name="${escapeHtml(f.name)}">${t('download')}</button><button class="ghost compact-button" type="button" data-rename-file="${f.file_id}">${t('rename')}</button><button class="ghost danger-text compact-button" type="button" data-delete-file="${f.file_id}">${t('delete')}</button></div></div>`).join('')||`<p class="muted">${t('noFiles')}</p>`;}
function renameSourceFile(fileId){const file=(state.bootstrap?.files||[]).find(item=>item.file_id===fileId);if(!file)return;inputAction({title:'重命名 Source File',label:'文件名',value:file.name,eyebrow:'Source Materials',acceptLabel:'保存'},async value=>{const updated=await api(`/api/files/${fileId}`,{method:'PATCH',body:JSON.stringify({name:value})});state.bootstrap.files=(state.bootstrap.files||[]).map(item=>item.file_id===fileId?updated:item);renderFileLibrary();renderRequestFiles();toast('文件已重命名');});}
function renderConversationSourcePicker(){const selected=new Set(state.conversation.source_manifest?.filter(x=>x.availability==='available').map(x=>x.file_id)||[]);$('#conversation-source-picker').innerHTML=(state.bootstrap.files||[]).map(f=>`<label class="file-check"><input type="checkbox" value="${f.file_id}" ${selected.has(f.file_id)?'checked':''}><span>${escapeHtml(f.name)}</span></label>`).join('')||'<p class="muted">无可用文件。</p>';}

function openEntityContextMenu(event, entity) {
  state.contextEntity = entity;
  const menu = $('#entity-context-menu');
  $('#context-archive').classList.toggle(
    'hidden', entity.type !== 'conversation' || !(state.bootstrap.projects || []).length
  );
  menu.classList.remove('hidden');
  menu.style.left = `${event.clientX}px`; menu.style.top = `${event.clientY}px`;
  const rect = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(event.clientX, window.innerWidth - rect.width - 8));
  const top = Math.max(8, Math.min(event.clientY, window.innerHeight - rect.height - 8));
  menu.style.left = `${left}px`; menu.style.top = `${top}px`;
  $('#context-rename').focus({preventScroll:true});
}
function closeEntityContextMenu() {
  $('#entity-context-menu')?.classList.add('hidden');
  state.contextEntity = null;
}
function renameContextEntity() {
  const entity = state.contextEntity; closeEntityContextMenu(); if (!entity) return;
  if (entity.type === 'project') openProjectManager(entity.id);
  else openConversationManager(entity.id);
}
function archiveContextEntity() {
  const entity = state.contextEntity;
  closeEntityContextMenu();
  if (!entity || entity.type !== 'conversation') return;
  const projects = state.bootstrap.projects || [];
  if (!projects.length) {
    toast('请先新建一个 Project', true);
    return;
  }
  state.archivingConversationId = entity.id;
  $('#archive-conversation-project').innerHTML = projects
    .map(project => `<option value="${project.project_id}">${escapeHtml(project.name)}</option>`)
    .join('');
  openDialog('#archive-conversation-dialog');
}
async function submitArchiveConversation(event) {
  event.preventDefault();
  const conversationId = state.archivingConversationId;
  const projectId = $('#archive-conversation-project').value;
  if (!conversationId || !projectId) return;
  try {
    await api(`/api/conversations/${conversationId}/move`, {
      method:'POST', body:JSON.stringify({project_id:projectId}),
    });
    closeDialog('#archive-conversation-dialog');
    state.archivingConversationId = null;
    if (state.conversation?.conversation_id === conversationId) state.conversation.project_id = projectId;
    await refreshBootstrap({keepConversation:true});
    toast(`已归档到 ${projectName(projectId)}`);
  } catch (error) {
    toast(error.message, true);
  }
}
function deleteContextEntity() {
  const entity = state.contextEntity; closeEntityContextMenu(); if (!entity) return;
  if (entity.type === 'project') requestDeleteProject(entity.id, false);
  else requestDeleteConversation(entity.id, false);
}

function createNewProject(){
  $('#entity-eyebrow').textContent='Project'; $('#entity-title').textContent='新建 Project'; $('#entity-name').value=''; openDialog('#entity-dialog');
  requestAnimationFrame(()=>$('#entity-name').focus({preventScroll:true}));
}
function openProjectManager(id){const p=state.bootstrap.projects.find(x=>x.project_id===id);if(!p)return;state.managingProjectId=id;$('#project-manage-name').value=p.name;openDialog('#project-manage-dialog');requestAnimationFrame(()=>{$('#project-manage-name').focus({preventScroll:true});$('#project-manage-name').select();});}
async function saveManagedProject(){try{await api(`/api/projects/${state.managingProjectId}`,{method:'PATCH',body:JSON.stringify({name:$('#project-manage-name').value})});closeDialog('#project-manage-dialog');await refreshBootstrap();}catch(error){toast(error.message,true);}}
function trackDeletion(jobId){
  if(!jobId||state.deletionJobs.has(jobId))return;
  const timer=setInterval(async()=>{try{const job=await api(`/api/deletion-jobs/${jobId}`);if(['completed','failed'].includes(job.status)){clearInterval(timer);state.deletionJobs.delete(jobId);await refreshBootstrap({keepConversation:job.status==='failed'});if(job.status==='failed')showAlert('终止并删除失败',job.error||'后台删除任务失败，可以重新尝试。');}}catch(error){clearInterval(timer);state.deletionJobs.delete(jobId);console.warn(error);}},1000);
  state.deletionJobs.set(jobId,timer);
}
async function deleteEntity(path,{closeManager,wasCurrent}){
  const result=await api(path,{method:'DELETE'});
  if(closeManager)closeDialog(closeManager);
  if(result.deletion_job_id){if(wasCurrent)clearIdentityScopedDom();trackDeletion(result.deletion_job_id);await refreshBootstrap({keepConversation:!wasCurrent});return;}
  if(wasCurrent)clearIdentityScopedDom();
  await refreshBootstrap({keepConversation:!wasCurrent});
}
function requestDeleteProject(id, closeManager=true){confirmAction('终止并永久删除 Project','将终止仍在生成或研究中的任务，随后永久删除该 Project、全部 Contract、Research Runs 与数学产物。Usage/Credit Ledger 会保留。',async()=>{try{await deleteEntity(`/api/projects/${id}?confirm=permanently-delete-project-and-all-chats-and-tasks&terminate_active=true`,{closeManager:closeManager?'#project-manage-dialog':null,wasCurrent:Boolean(state.conversation?.project_id===id)});}catch(error){showAlert('删除 Project 失败',error.message);}},'终止并删除');}
function deleteManagedProject(){requestDeleteProject(state.managingProjectId,true);}
function openConversationManager(id=state.conversation?.conversation_id){const c=state.bootstrap.conversations.find(x=>x.conversation_id===id);if(!c)return;state.managingConversationId=id;$('#conversation-manage-name').value=c.display_name;$('#conversation-manage-project').value=c.project_id||'';const current=id===state.conversation?.conversation_id?selectedContractData():null;state.managingContractVersion=current?.kind==='version'?Number(current.version):null;const button=$('#delete-conversation');button.disabled=!state.managingContractVersion||isConversationLocked();button.textContent=state.managingContractVersion?`删除当前 Contract v${state.managingContractVersion}`:'请先选择 Contract Version';button.title=state.managingContractVersion?'只删除当前版本，不删除整个分支':'Working Draft 请使用“放弃草稿”';openDialog('#conversation-manage-dialog');requestAnimationFrame(()=>{$('#conversation-manage-name').focus({preventScroll:true});$('#conversation-manage-name').select();});}
async function saveManagedConversation(){const id=state.managingConversationId;if(!id)return;try{await api(`/api/conversations/${id}`,{method:'PATCH',body:JSON.stringify({name:$('#conversation-manage-name').value})});await api(`/api/conversations/${id}/move`,{method:'POST',body:JSON.stringify({project_id:$('#conversation-manage-project').value||null})});closeDialog('#conversation-manage-dialog');await refreshBootstrap({keepConversation:true});if(state.conversation?.conversation_id===id)await reloadConversation();}catch(error){toast(error.message,true);}}
function requestDeleteConversation(id,closeManager=true){confirmAction('终止并永久删除 Contract Conversation','将终止仍在生成或研究中的任务，随后永久删除该对话、Contract Versions、Research Runs 与数学产物。Usage/Credit Ledger 会保留。',async()=>{try{await deleteEntity(`/api/conversations/${id}?confirm=permanently-delete-contract-conversation&terminate_active=true`,{closeManager:closeManager?'#conversation-manage-dialog':null,wasCurrent:state.conversation?.conversation_id===id});}catch(error){showAlert('删除 Contract Conversation 失败',error.message);}},'终止并删除');}
function deleteManagedConversation(){const id=state.managingConversationId;const version=state.managingContractVersion;if(!id||!version)return;confirmAction(`永久删除 Contract v${version}`,'只删除当前 immutable Contract Version、该版本的 Contract Chat 与对应 Markdown。其他版本和整个 Conversation 分支都会保留。',async()=>{try{const result=await api(`/api/conversations/${id}/versions/${version}?confirm=permanently-delete-current-contract-version`,{method:'DELETE'});closeDialog('#conversation-manage-dialog');state.managingContractVersion=null;await refreshBootstrap({keepConversation:true});if(result.latest_contract_version>0){setContractSelection({kind:'version',version:Number(result.latest_contract_version)});await reloadConversation();toast(`Contract v${version} 已删除，当前切换到 v${result.latest_contract_version}`);}else{state.contractSelections.delete(id);state.conversation=null;state.selected={kind:'version',version:null};await openConversation(id);toast(`Contract v${version} 已删除，Conversation 已回到 Research Request 草稿`);}}catch(error){showAlert('删除 Contract Version 失败',error.message);}},'删除当前版本');}

async function requestOtp(event){event.preventDefault();const email=$('#login-email').value.trim();try{const result=await api('/api/auth/request-otp',{method:'POST',body:JSON.stringify({email})},{allowUnauthorized:true});state.otpEmail=email;$('#otp-request-form').classList.add('hidden');$('#otp-verify-form').classList.remove('hidden');if(result.dev_code){$('#otp-dev-hint').textContent=`DEV code: ${result.dev_code}`;$('#otp-dev-hint').classList.remove('hidden');}toast('验证码已发送');}catch(error){toast(error.message,true);}}
async function verifyOtp(event){event.preventDefault();try{await api('/api/auth/verify-otp',{method:'POST',body:JSON.stringify({email:state.otpEmail,code:$('#login-code').value})},{allowUnauthorized:true});resetOtpForm();await refreshBootstrap({keepConversation:false});}catch(error){toast(error.message,true);}}
function resetOtpForm(){$('#otp-request-form').classList.remove('hidden');$('#otp-verify-form').classList.add('hidden');$('#login-code').value='';$('#otp-dev-hint').classList.add('hidden');}
async function logout(){try{await api('/api/auth/logout',{method:'POST'});}catch(_){}clearIdentityScopedDom();state.bootstrap=null;closeDialog('#user-dialog');window.location.replace('/login');}

async function openAdmin(){try{await refreshAdmin();openDialog('#admin-dialog');}catch(error){toast(error.message,true);}}
async function refreshAdmin(){const [users,groups,domains,ledger]=await Promise.all([api('/api/admin/users'),api('/api/admin/groups'),api('/api/admin/domains'),api('/api/admin/credit-ledger')]);state.admin={users,allUsers:users,groups,domains,ledger};renderAdminUsers();renderAdminGroups();renderAdminDomains();renderAdminLedger();}
function renderAdminUsers(){$('#admin-users').innerHTML=`<div class="admin-list">${state.admin.users.map(u=>`<article class="admin-row"><div><strong>${escapeHtml(u.display_name)}</strong><small>${escapeHtml(u.email||u.user_id)}</small></div><div class="chip-row"><span class="chip">${escapeHtml(u.role)}</span><span class="chip">${escapeHtml(u.status)}</span><span class="chip">credit ${money(u.credit_balance)}</span></div><div><button class="ghost compact-button" data-admin-credit="${u.user_id}">Credit</button><button class="ghost compact-button" data-admin-toggle-user="${u.user_id}" data-current-status="${u.status}">${u.status==='active'?'Disable':'Enable'}</button></div></article>`).join('')}</div>`;}
function renderAdminGroups(){
  $('#admin-groups').innerHTML=`<form id="admin-group-create" class="inline-admin-form"><input id="admin-group-name" required placeholder="New group"><button class="primary">Create</button></form><div class="admin-list">${state.admin.groups.map(g=>{
    const automatic=g.membership_mode==='automatic';
    const memberButton=automatic
      ? '<button class="ghost compact-button" type="button" disabled title="成员由邮箱策略自动维护">Automatic members</button>'
      : `<button class="ghost compact-button" type="button" data-group-manage-members="${g.group_id}">Manage members</button>`;
    return `<article class="admin-row"><div><strong>${escapeHtml(g.name)}</strong><small>${escapeHtml(g.description||g.group_id)}</small></div><div class="chip-row"><span class="chip">${g.member_count||0} members</span>${automatic?'<span class="chip">email policy</span>':''}</div><div>${memberButton}<button class="ghost compact-button" data-group-credit="${g.group_id}">Grant credit</button><button class="ghost danger-text compact-button" data-group-delete="${g.group_id}"${g.system_key?' disabled title="系统 Group 不能删除"':''}>Delete</button></div></article>`;
  }).join('')}</div>`;
  $('#admin-group-create')?.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/admin/groups',{method:'POST',body:JSON.stringify({name:$('#admin-group-name').value})});await refreshAdmin();}catch(error){toast(error.message,true);}});
}
function renderAdminDomains(){$('#admin-domains').innerHTML=`<form id="admin-domain-create" class="inline-admin-form"><input id="admin-domain-name" required placeholder="example.edu"><button class="primary">Add</button></form><div class="admin-list">${state.admin.domains.map(d=>`<article class="admin-row"><div><strong>${escapeHtml(d.domain)}</strong><small>${d.enabled?'enabled':'disabled'}</small></div><div></div><div><button class="ghost compact-button" data-domain-toggle="${d.domain_id}" data-domain-name="${escapeHtml(d.domain)}" data-domain-enabled="${d.enabled}">${d.enabled?'Disable':'Enable'}</button><button class="ghost danger-text compact-button" data-domain-delete="${d.domain_id}">Delete</button></div></article>`).join('')}</div>`;$('#admin-domain-create')?.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/admin/domains',{method:'POST',body:JSON.stringify({domain:$('#admin-domain-name').value,enabled:true})});await refreshAdmin();}catch(error){toast(error.message,true);}});}
function renderAdminLedger(){$('#admin-ledger').innerHTML=`<div class="admin-list">${state.admin.ledger.slice(0,200).map(x=>`<article class="admin-row"><div><strong>${x.amount>=0?'+':''}${money(x.amount)}</strong><small>${escapeHtml(x.user_id)} · ${escapeHtml(x.reason)}</small></div><div></div><time>${escapeHtml(new Date(x.created_at).toLocaleString())}</time></article>`).join('')||'<p class="muted">No credit events.</p>'}</div>`;}

function renderAdminUsers(){
  const affiliation=user=>['institution','department','position'].map(field=>`<small>${escapeHtml(t(field).replace(/ \(optional\)|（可选）/g,''))}: ${escapeHtml(user[field]||'—')}</small>`).join('');
  const consumption=user=>`<div class="admin-credit-windows"><strong>${t('consumedCredits')}</strong>${[['1d','creditWindow1d'],['7d','creditWindow7d'],['30d','creditWindow30d'],['all','creditWindowAll']].map(([window,label])=>`<span class="chip" data-credit-window="${window}">${t(label)}: ${money(user.consumed_credit_windows?.[window]??(window==='all'?user.total_consumed_credit:0))}</span>`).join('')}</div>`;
  const panel=$('#admin-users');
  panel.innerHTML=`<form id="admin-user-search" class="inline-admin-form"><input id="admin-user-search-email" type="search" placeholder="Search email"><button class="ghost">Search</button></form><form id="admin-user-create" class="inline-admin-form"><input id="admin-pending-email" type="email" required placeholder="Pre-create pending email"><input id="admin-pending-name" placeholder="Display name"><input id="admin-pending-credit" type="number" step="0.01" value="0" aria-label="Initial Credits"><button class="primary">Create pending user</button></form><div class="admin-list">${state.admin.users.map(u=>`<article class="admin-row"><div><strong>${escapeHtml(u.display_name)}</strong><small>${escapeHtml(u.email||u.user_id)} · ${escapeHtml(u.storage_key||'unassigned')} · ${(u.groups||[]).map(escapeHtml).join(', ')||'No group'}</small>${affiliation(u)}</div><div class="chip-row"><span class="chip">${escapeHtml(u.role)}</span>${u.is_developer?'<span class="chip">developer</span>':''}<span class="chip">${escapeHtml(u.status)}</span><span class="chip">${t('creditBalance')}: ${money(u.credit_balance)}</span>${consumption(u)}<span class="chip">${formatBytes(u.durable_storage_bytes||0)} / ${u.storage_quota_bytes==null?'Unlimited':formatBytes(u.storage_quota_bytes)}</span></div><div><button class="ghost compact-button" data-admin-credit="${u.user_id}">Credit</button><button class="ghost compact-button" data-admin-developer="${u.user_id}" data-current-developer="${u.is_developer}">${u.is_developer?'Remove developer':'Make developer'}</button><button class="ghost compact-button" data-admin-quota="${u.user_id}" data-current-quota="${u.storage_quota_bytes??''}">Quota</button><button class="ghost compact-button" data-admin-groups="${u.user_id}">Group</button><button class="ghost compact-button" data-admin-toggle-user="${u.user_id}" data-current-status="${u.status}">${u.status==='active'?'Disable':'Enable'}</button>${u.status==='pending_verification'?`<button class="ghost danger-text compact-button" data-admin-delete-pending="${u.user_id}">Delete pending</button>`:''}</div></article>`).join('')}</div>`;
  $('#admin-user-search')?.addEventListener('submit',async event=>{event.preventDefault();try{state.admin.users=await api(`/api/admin/users?email=${encodeURIComponent($('#admin-user-search-email').value.trim())}`);renderAdminUsers();}catch(error){toast(error.message,true);}});
  $('#admin-user-create')?.addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/admin/users',{method:'POST',body:JSON.stringify({email:$('#admin-pending-email').value,display_name:$('#admin-pending-name').value||null,initial_credit:Number($('#admin-pending-credit').value||0)})});await refreshAdmin();}catch(error){toast(error.message,true);}});
}

function adminGroupIsAutomatic(group){return group.membership_mode==='automatic';}
function groupContainsUser(group,userId){return (group.members||[]).some(member=>member.user_id===userId);}

function openUserGroupEditor(userId){
  const user=state.admin.users.find(item=>item.user_id===userId);if(!user)return;
  state.adminMembership={mode:'user',targetId:userId,selected:new Set(state.admin.groups.filter(group=>groupContainsUser(group,userId)).map(group=>group.group_id)),query:''};
  $('#admin-membership-title').textContent=`${user.display_name} 的 Groups`;
  $('#admin-membership-description').textContent='邮箱策略自动维护的 Group 只读；你可以直接勾选这个用户所属的自定义 Group。';
  $('#admin-membership-search').placeholder='搜索 Group 名称';$('#admin-membership-search').value='';
  renderAdminMembershipList();openDialog('#admin-membership-dialog');
}

function openGroupMemberEditor(groupId){
  const group=state.admin.groups.find(item=>item.group_id===groupId);if(!group||adminGroupIsAutomatic(group))return;
  state.adminMembership={mode:'group',targetId:groupId,selected:new Set((group.members||[]).map(member=>member.user_id)),query:''};
  $('#admin-membership-title').textContent=`管理 ${group.name} 成员`;
  $('#admin-membership-description').textContent='按姓名或邮箱搜索并勾选成员；保存后一次同步本组成员关系。';
  $('#admin-membership-search').placeholder='搜索姓名或邮箱';$('#admin-membership-search').value='';
  renderAdminMembershipList();openDialog('#admin-membership-dialog');
}

function renderAdminMembershipList(){
  const editor=state.adminMembership;const query=editor.query.trim().toLowerCase();let items=[];
  if(editor.mode==='user'){
    items=state.admin.groups.filter(group=>!query||`${group.name} ${group.description||''}`.toLowerCase().includes(query)).map(group=>({id:group.group_id,title:group.name,detail:adminGroupIsAutomatic(group)?'邮箱策略自动归属':(group.description||'自定义 Group'),readonly:adminGroupIsAutomatic(group),checked:editor.selected.has(group.group_id)}));
  }else if(editor.mode==='group'){
    items=(state.admin.allUsers||state.admin.users).filter(user=>!query||`${user.display_name} ${user.email||''} ${user.storage_key||''}`.toLowerCase().includes(query)).map(user=>({id:user.user_id,title:user.display_name,detail:`${user.email||user.user_id} · ${user.status}`,readonly:false,checked:editor.selected.has(user.user_id)}));
  }
  $('#admin-membership-list').innerHTML=items.length?items.map(item=>`<label class="admin-membership-option${item.readonly?' readonly':''}"><input type="checkbox" data-admin-membership-choice="${escapeHtml(item.id)}"${item.checked?' checked':''}${item.readonly?' disabled':''}><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.detail)}</small></span>${item.readonly?'<span class="admin-membership-lock">自动管理</span>':''}</label>`).join(''):'<p class="empty-state">没有匹配项。</p>';
}

async function saveAdminMembership(event){
  event.preventDefault();const editor=state.adminMembership;if(!editor.mode||!editor.targetId)return;
  await withBusy(event.submitter||$('#admin-membership-save'),async()=>{
    try{
      if(editor.mode==='user'){
        const groupIds=state.admin.groups.filter(group=>!adminGroupIsAutomatic(group)&&editor.selected.has(group.group_id)).map(group=>group.group_id);
        await api(`/api/admin/users/${editor.targetId}/groups`,{method:'PUT',body:JSON.stringify({group_ids:groupIds})});
      }else{
        await api(`/api/admin/groups/${editor.targetId}/members`,{method:'PUT',body:JSON.stringify({user_ids:[...editor.selected]})});
      }
      await refreshAdmin();closeDialog('#admin-membership-dialog');toast('Group 成员关系已更新');
    }catch(error){showAlert('更新 Group 失败',error.message);}
  });
}

function renderAdminDomains(){
  const options=state.admin.groups.map(group=>`<option value="${group.group_id}">${escapeHtml(group.name)}</option>`).join('');
  $('#admin-domains').innerHTML=`<form id="admin-domain-create" class="inline-admin-form"><input id="admin-domain-name" required placeholder="example.edu"><select id="admin-domain-group">${options}</select><input id="admin-domain-credit" type="number" min="0" step="0.01" value="0" aria-label="Welcome Credits"><button class="primary">Add policy</button></form><div class="admin-list">${state.admin.domains.map(d=>`<article class="admin-row"><div><strong>${escapeHtml(d.domain)}</strong><small>${d.enabled?'enabled':'disabled'} · ${money(d.welcome_credit||0)}</small></div><div class="chip-row"><span class="chip">${escapeHtml(state.admin.groups.find(g=>g.group_id===d.group_id)?.name||d.group_id)}</span></div><div><button class="ghost compact-button" data-domain-toggle="${d.domain_id}" data-domain-name="${escapeHtml(d.domain)}" data-domain-enabled="${d.enabled}" data-domain-group="${d.group_id}" data-domain-credit="${d.welcome_credit||0}" data-domain-grant="${escapeHtml(d.grant_key||'')}">${d.enabled?'Disable':'Enable'}</button><button class="ghost danger-text compact-button" data-domain-delete="${d.domain_id}">Delete</button></div></article>`).join('')}</div>`;
  $('#admin-domain-create')?.addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/admin/domains',{method:'POST',body:JSON.stringify({domain:$('#admin-domain-name').value,group_id:$('#admin-domain-group').value,welcome_credit:Number($('#admin-domain-credit').value||0),enabled:true})});await refreshAdmin();}catch(error){toast(error.message,true);}});
}

// Delegated UI actions
document.addEventListener('click',async event=>{
  if (Date.now() < state.navigationDrag.suppressClickUntil && event.target.closest('.entity-row')) { event.preventDefault(); return; }
  if(!event.target.closest('#entity-context-menu'))closeEntityContextMenu();
  const close=event.target.closest('[data-close]');if(close){const dialog=close.closest('dialog');if(dialog?.id==='section-dialog'){await finishSectionEditor();return;}if(dialog?.id==='request-editor-dialog')await finishRequestEditor();if(dialog?.id==='new-contract-dialog'&&state.request.conversationId)await saveRequestDraft({silent:true});dialog?.close();return;}
  const newContract=event.target.closest('[data-action="new-contract"],#new-contract');if(newContract){await openNewContract();return;}
  const projectMenu=event.target.closest('[data-project-menu]');if(projectMenu){event.preventDefault();event.stopPropagation();openProjectManager(projectMenu.dataset.projectMenu);return;}
  const chat=event.target.closest('[data-conversation-id]');if(chat){await openConversation(chat.dataset.conversationId);return;}
  const download=event.target.closest('[data-download-file]');if(download){try{await downloadAuthenticated(`/api/files/${download.dataset.downloadFile}/download`,download.dataset.fileName||'source-file');}catch(error){toast(error.message,true);}return;}
  const renameFileButton=event.target.closest('[data-rename-file]');if(renameFileButton){renameSourceFile(renameFileButton.dataset.renameFile);return;}
  const del=event.target.closest('[data-delete-file]');if(del){confirmAction('删除 Source File','历史 manifest 会显示文件缺失；已执行结果不受影响。',async()=>{try{await api(`/api/files/${del.dataset.deleteFile}`,{method:'DELETE'});await refreshBootstrap();}catch(error){toast(error.message,true);}},'删除文件');return;}
  const sw=event.target.closest('[data-user-id]');if(sw){if(state.request.conversationId)await saveRequestDraft({silent:true});await api('/api/dev/switch-user',{method:'POST',body:JSON.stringify({user_id:sw.dataset.userId})});clearIdentityScopedDom();closeDialog('#user-dialog');await refreshBootstrap({keepConversation:false});return;}
  const tab=event.target.closest('[data-admin-tab]');if(tab){$$('.admin-tabs button').forEach(b=>b.classList.toggle('active',b===tab));$$('.admin-panel').forEach(p=>p.classList.add('hidden'));$(`#admin-${tab.dataset.adminTab}`).classList.remove('hidden');return;}
  const credit=event.target.closest('[data-admin-credit]');if(credit){inputAction({title:'调整用户 Credit',label:'Credit amount',value:'20',eyebrow:'Administration'},async value=>{const amount=Number(value);if(!Number.isFinite(amount))throw new Error('请输入有效数字');await api(`/api/admin/users/${credit.dataset.adminCredit}/credit`,{method:'POST',body:JSON.stringify({amount,reason:'admin grant'})});await refreshAdmin();await refreshBootstrap();});return;}
  const toggle=event.target.closest('[data-admin-toggle-user]');if(toggle){try{await api(`/api/admin/users/${toggle.dataset.adminToggleUser}`,{method:'PATCH',body:JSON.stringify({status:toggle.dataset.currentStatus==='active'?'disabled':'active'})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const developer=event.target.closest('[data-admin-developer]');if(developer){try{await api(`/api/admin/users/${developer.dataset.adminDeveloper}`,{method:'PATCH',body:JSON.stringify({is_developer:developer.dataset.currentDeveloper!=='true'})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const quota=event.target.closest('[data-admin-quota]');if(quota){inputAction({title:'调整 Durable Storage Quota',label:'Quota bytes',value:quota.dataset.currentQuota||'536870912',eyebrow:'Administration'},async value=>{const storage_quota_bytes=Number(value);if(!Number.isInteger(storage_quota_bytes)||storage_quota_bytes<0)throw new Error('请输入非负整数字节数');await api(`/api/admin/users/${quota.dataset.adminQuota}`,{method:'PATCH',body:JSON.stringify({storage_quota_bytes})});await refreshAdmin();});return;}
  const userGroups=event.target.closest('[data-admin-groups]');if(userGroups){openUserGroupEditor(userGroups.dataset.adminGroups);return;}
  const deletePending=event.target.closest('[data-admin-delete-pending]');if(deletePending){confirmAction('永久删除 pending 用户','仅当该用户从未验证且没有 Conversation、File、Run 或 Usage 时允许删除；Credit 与 Admin 审计快照仍保留。',async()=>{try{await api(`/api/admin/users/${deletePending.dataset.adminDeletePending}`,{method:'DELETE'});await refreshAdmin();}catch(error){showAlert('删除 pending 用户失败',error.message);}},'删除 pending 用户');return;}
  const manageMembers=event.target.closest('[data-group-manage-members]');if(manageMembers){openGroupMemberEditor(manageMembers.dataset.groupManageMembers);return;}
  const groupCredit=event.target.closest('[data-group-credit]');if(groupCredit){inputAction({title:'为 Group 发放 Credit',label:'Credit per member',value:'20',eyebrow:'Administration'},async value=>{const amount=Number(value);if(!Number.isFinite(amount))throw new Error('请输入有效数字');await api(`/api/admin/groups/${groupCredit.dataset.groupCredit}/credit`,{method:'POST',body:JSON.stringify({amount,reason:'group grant'})});await refreshAdmin();await refreshBootstrap();});return;}
  const groupDelete=event.target.closest('[data-group-delete]');if(groupDelete){confirmAction('删除 Group','将永久删除这个 Group；用户账户与 Usage Ledger 不受影响。',async()=>{try{await api(`/api/admin/groups/${groupDelete.dataset.groupDelete}`,{method:'DELETE'});await refreshAdmin();}catch(error){showAlert('删除 Group 失败',error.message);}},'删除');return;}
  const domainToggle=event.target.closest('[data-domain-toggle]');if(domainToggle){try{await api('/api/admin/domains',{method:'POST',body:JSON.stringify({domain:domainToggle.dataset.domainName,group_id:domainToggle.dataset.domainGroup,welcome_credit:Number(domainToggle.dataset.domainCredit||0),grant_key:domainToggle.dataset.domainGrant||null,enabled:domainToggle.dataset.domainEnabled!=='true'})});await refreshAdmin();}catch(error){toast(error.message,true);}return;}
  const domainDelete=event.target.closest('[data-domain-delete]');if(domainDelete){confirmAction('删除邮箱域名策略','删除后，该域名仍可注册，但会按 External 用户处理且不再获得该策略的 Welcome Credit。',async()=>{try{await api(`/api/admin/domains/${domainDelete.dataset.domainDelete}`,{method:'DELETE'});await refreshAdmin();}catch(error){showAlert('删除域名策略失败',error.message);}},'删除');}
});
document.addEventListener('change',event=>{const choice=event.target.closest('[data-admin-membership-choice]');if(!choice||choice.disabled)return;if(choice.checked)state.adminMembership.selected.add(choice.dataset.adminMembershipChoice);else state.adminMembership.selected.delete(choice.dataset.adminMembershipChoice);});
document.addEventListener('dblclick',event=>{const requestPreview=event.target.closest('[data-request-preview]');if(requestPreview){event.preventDefault();openRequestEditor(requestPreview.dataset.requestPreview);return;}if(event.target.closest('.contract-title h1,#page-title')&&state.conversation){event.preventDefault();openConversationManager();return;}const fragment=event.target.closest('.contract-subsection-card[data-editable="true"]');if(fragment){event.preventDefault();openSectionEditor(fragment.dataset.sectionKey,fragment.dataset.fragmentId);return;}const s=event.target.closest('.contract-section[data-editable="true"]');if(s)openSectionEditor(s.dataset.sectionKey);});
document.addEventListener('contextmenu',event=>{if(!event.target.closest('.entity-row'))closeEntityContextMenu();});
document.addEventListener('pointermove',handleNavigationPointerMove,{passive:false});
document.addEventListener('pointerup',handleNavigationPointerEnd);
document.addEventListener('pointercancel',handleNavigationPointerEnd);
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeEntityContextMenu();});
window.addEventListener('resize',closeEntityContextMenu);
window.addEventListener('scroll',closeEntityContextMenu,true);

// Direct wiring
$('#new-contract').onclick=openNewContract;
$('#home-brand').onclick=goHome;
$('#new-contract-form').addEventListener('submit',generateFromRequest);
$('#save-request-draft').onclick=()=>saveRequestDraft({silent:false});
for(const id of ['request-problem','request-background','request-methods','request-partial','request-ideas','request-profile','request-project','request-contract-language']){$(`#${id}`).addEventListener('input',scheduleRequestAutosave);$(`#${id}`).addEventListener('change',scheduleRequestAutosave);}
$('#request-profile').addEventListener('change',renderRequestGenerationEstimate);
for(const [key,spec] of Object.entries(REQUEST_FIELDS)){$(`#${spec.id}`).addEventListener('blur',()=>renderRequestBlock(key));const preview=$(`[data-request-preview="${key}"]`);preview.addEventListener('click',event=>event.preventDefault());preview.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();openRequestEditor(key);}});}
const requestDropzone=$('#request-file-dropzone');const requestFileInput=$('#request-file-input');
setupFileDropzone(requestDropzone,requestFileInput,uploadRequestFiles);
const libraryDropzone=$('#library-file-dropzone');const libraryFileInput=$('#file-input');
setupFileDropzone(libraryDropzone,libraryFileInput,uploadLibraryFiles);
$('#request-library-file').addEventListener('change',event=>{addLibraryFileToRequest(event.target.value);event.target.value='';});
$('#new-project').onclick=createNewProject;
$('#help-button').onclick=()=>openDialog('#help-dialog');
$('#files-button').onclick=()=>openDialog('#files-dialog');
$('#user-card').onclick=async()=>{openDialog('#user-dialog');try{await refreshUsageWindow();}catch(error){toast(error.message,true);}};
$('#usage-window').addEventListener('change',()=>refreshUsageWindow().catch(error=>toast(error.message,true)));
$('#admin-membership-search').addEventListener('input',event=>{state.adminMembership.query=event.target.value;renderAdminMembershipList();});
$('#admin-membership-form').addEventListener('submit',saveAdminMembership);
$('#contract-sources').onclick=()=>{renderConversationSourcePicker();openDialog('#sources-dialog');};
$('#version-select').onchange=async e=>{rememberContractScroll();const [kind,value]=e.target.value.split(':');setContractSelection(kind==='draft'?{kind:'draft',version:null}:(kind==='request'?{kind:'request',version:null}:{kind,version:Number(value)}));await renderConversation();};
$('#save-contract-version').onclick=commitDraft;
$('#discard-working-draft').onclick=discardDraft;
$('#clone-contract').onclick=branchFromCurrentSelection;
$('#back-to-run').onclick=returnToRun;
$('#conversation-menu').onclick=()=>openConversationManager();
$('#approve-contract').onclick=()=>approveContract(false);
$('#full-regenerate').onclick=fullRegenerate;
$('#resume-contract-generation').onclick=resumeContractGeneration;
$('#regenerate-form').addEventListener('submit',submitFullRegeneration);
$('#regenerate-instruction').addEventListener('blur',()=>previewRegenerationInstruction());
$('#regenerate-instruction-preview').addEventListener('dblclick',editRegenerationInstruction);
$('#regenerate-instruction-preview').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();editRegenerationInstruction();}});
$('#contract-chat-form').addEventListener('submit',sendContractChat);
$('#contract-chat-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();event.currentTarget.form?.requestSubmit();}});
$('#section-editor').addEventListener('input',()=>{scheduleSectionPreview();scheduleSectionAutosave();});
$('#section-editor').addEventListener('blur',()=>saveCurrentSection({silent:true}));
$('#fragment-title').addEventListener('input',()=>{scheduleSectionPreview();scheduleSectionAutosave();});
$('#fragment-title').addEventListener('blur',()=>saveCurrentSection({silent:true}));
$('#section-dialog').addEventListener('cancel',event=>{event.preventDefault();finishSectionEditor();});
$('#request-editor-source').addEventListener('input',()=>{updateRequestFromEditor();scheduleRequestEditorPreview();});
$('#request-editor-dialog').addEventListener('cancel',event=>{event.preventDefault();finishRequestEditor().finally(()=>closeDialog('#request-editor-dialog'));});
$('#save-conversation-sources').onclick=async()=>{const ids=$$('#conversation-source-picker input:checked').map(i=>i.value);try{await api(`/api/conversations/${state.conversation.conversation_id}/sources`,{method:'PUT',body:JSON.stringify({file_ids:ids})});closeDialog('#sources-dialog');await reloadConversation();toast('Source Materials 已更新');}catch(error){toast(error.message,true);}};
$('#preferences-form').addEventListener('submit',async e=>{e.preventDefault();try{const updated=await api('/api/users/me/preferences',{method:'PATCH',body:JSON.stringify({institution:$('#pref-institution').value,department:$('#pref-department').value,position:$('#pref-position').value,display_name:$('#pref-name').value,website_language:$('#pref-website-language').value,default_contract_language:$('#pref-contract-language').value,default_research_language:$('#pref-research-language').value,default_model_strategy:$('#pref-model').value,default_budget_preset:$('#pref-budget').value})});if(state.bootstrap)state.bootstrap.current_user=updated;applyWebsiteLanguage(updated.website_language);await refreshBootstrap();if(state.activeView==='conversation')await renderConversation();else if(state.activeView==='run')await renderRun();toast(uiLanguage()==='en'?'Preferences saved':'Preferences 已保存');}catch(error){toast(error.message,true);}});
$('#confirm-cancel').onclick=()=>closeDialog('#confirm-dialog');
$('#confirm-accept').onclick=async event=>{const cb=state.confirmCallback;state.confirmCallback=null;closeDialog('#confirm-dialog');if(cb)await withBusy(event.currentTarget,cb);};
$('#project-manage-form').addEventListener('submit',event=>{event.preventDefault();withBusy(event.submitter||$('#save-project'),saveManagedProject);});
$('#conversation-manage-form').addEventListener('submit',event=>{event.preventDefault();withBusy(event.submitter||$('#save-conversation'),saveManagedConversation);});
$('#delete-project').onclick=deleteManagedProject;$('#delete-conversation').onclick=deleteManagedConversation;
$('#context-rename').onclick=renameContextEntity;$('#context-archive').onclick=archiveContextEntity;$('#context-delete').onclick=deleteContextEntity;
$('#archive-conversation-form').addEventListener('submit',submitArchiveConversation);
$('#preview-paper').onclick=async()=>{try{await previewFinalPaper();}catch(error){toast(error.message,true);}};
$('#developer-runtime').onclick=openDeveloperRuntime;
$('#runtime-keep-toggle').onclick=async()=>{if(!state.run)return;try{const keep=$('#runtime-keep-toggle').dataset.keep!=='true';await api(`/api/developer/runs/${state.run.run_id}/keep-runtime`,{method:'PATCH',body:JSON.stringify({keep})});await openDeveloperRuntime();}catch(error){toast(error.message,true);}};
$('#runtime-gc-dry-run').onclick=async()=>{if(!state.run)return;try{const result=await api(`/api/developer/runs/${state.run.run_id}/runtime-gc/dry-run`,{method:'POST'});showAlert('Runtime GC dry-run',`${result.files} files · ${formatBytes(result.bytes)} · ${result.eligible?'eligible':'not eligible'}`);}catch(error){toast(error.message,true);}};
$('#download-paper').onclick=async()=>{if(!state.run?.final_paper_pdf_path)return;try{await downloadAuthenticated(`/api/runs/${state.run.run_id}/final-paper/download`,`${state.run.run_id}-paper.pdf`);}catch(error){toast(error.message,true);}};
$('#download-paper-source').onclick=async()=>{if(!state.run?.final_paper_tex_path)return;try{await downloadAuthenticated(`/api/runs/${state.run.run_id}/final-paper/source`,`${state.run.run_id}-paper.tex`);}catch(error){toast(error.message,true);}};
$('#download-legacy-paper').onclick=async()=>{if(!state.run?.final_report_path)return;try{await downloadAuthenticated(`/api/runs/${state.run.run_id}/legacy-final-report`,`${state.run.run_id}-legacy-paper.md`);}catch(error){toast(error.message,true);}};
$('#paper-preview-dialog').addEventListener('close',()=>{if(state.paperPreviewUrl){URL.revokeObjectURL(state.paperPreviewUrl);state.paperPreviewUrl=null;}$('#paper-preview-frame').removeAttribute('src');});
for(const eventName of ['mousedown','auxclick'])$('#paper-preview-frame').addEventListener(eventName,event=>{if(event.button===1)event.preventDefault();});
$('#paper-preview-frame').addEventListener('pointerleave',event=>{if(event.buttons===0)event.currentTarget.blur();});
$('#view-locked-contract').onclick=viewLockedRunContract;
$('#pause-resume-run').onclick=pauseOrResumeRun;
$('#graceful-drain-run').onclick=requestGracefulDrain;
$('#otp-request-form').addEventListener('submit',requestOtp);$('#otp-verify-form').addEventListener('submit',verifyOtp);$('#otp-back').onclick=resetOtpForm;$('#logout-button').onclick=logout;$('#open-admin').onclick=openAdmin;
$('#close-generation-notice').onclick=()=>{state.generationNoticeClosed=true;$('#generation-notice').classList.add('hidden');};
$('#contract-chat-new').onclick=()=>{const list=$('#contract-chat-list');list.scrollTo({top:list.scrollHeight,behavior:'smooth'});$('#contract-chat-new').classList.add('hidden');};
$('#contract-chat-list').addEventListener('scroll',()=>{const list=$('#contract-chat-list');if(list.scrollHeight-list.scrollTop-list.clientHeight<72)$('#contract-chat-new').classList.add('hidden');});
$('#event-new-count').onclick=()=>{const list=$('#event-list');list.scrollTo({top:list.scrollHeight,behavior:'smooth'});state.runUi.eventUnread=0;updateEventUnread();};
$('#event-list').addEventListener('scroll',()=>{if(eventListNearBottom()&&state.runUi.eventUnread){state.runUi.eventUnread=0;updateEventUnread();}},{passive:true});
$('#node-grid').addEventListener('scroll',()=>{state.runUi.graphInteractionVersion+=1;},{passive:true});
$('#claim-new-count').onclick=()=>{const first=$('#claim-list .claim-item.new-result');first?.scrollIntoView({behavior:'smooth',block:'nearest'});$$('#claim-list .claim-item.new-result').forEach(item=>item.classList.remove('new-result'));state.runUi.claimUnread=0;updateClaimUnread();};
$('#claim-list').addEventListener('click',event=>{const card=event.target.closest('[data-claim-id]');if(card)openVerifiedClaim(card.dataset.claimId);});
$('#claim-list').addEventListener('keydown',event=>{if(!['Enter',' '].includes(event.key))return;const card=event.target.closest('[data-claim-id]');if(card){event.preventDefault();openVerifiedClaim(card.dataset.claimId);}});
$('#verified-dialog-proof-toggle').onclick=toggleVerifiedClaimProof;
$('#contract-main').addEventListener('scroll',()=>{rememberContractScroll();scheduleContractOutlineUpdate();},{passive:true});
window.addEventListener('resize',()=>scheduleContractOutlineUpdate(true),{passive:true});
$('#login-dialog').addEventListener('cancel',e=>{if(!state.bootstrap)e.preventDefault();});
$('#entity-form').addEventListener('submit',async e=>{e.preventDefault();const name=$('#entity-name').value.trim();if(!name)return;await withBusy(e.submitter||$('#entity-form button[type="submit"]'),async()=>{try{await api('/api/projects',{method:'POST',body:JSON.stringify({name})});closeDialog('#entity-dialog');await refreshBootstrap();}catch(error){showAlert('创建 Project 失败',error.message);}});});
$('#input-form').addEventListener('submit',async e=>{e.preventDefault();const value=$('#input-value').value.trim();if(!value)return;const callback=state.inputCallback;await withBusy(e.submitter||$('#input-accept'),async()=>{try{if(callback)await callback(value);state.inputCallback=null;closeDialog('#input-dialog');}catch(error){showAlert('操作失败',error.message);}});});

setupScrollSync();
setupRequestScrollSync();
setupDragDivider($('#section-splitter'),ratio=>{$('#section-split').style.gridTemplateColumns=`minmax(260px,${ratio}fr) 6px minmax(260px,${1-ratio}fr)`;});
setupDragDivider($('#request-editor-splitter'),ratio=>{$('#request-editor-split').style.gridTemplateColumns=`minmax(260px,${ratio}fr) 6px minmax(260px,${1-ratio}fr)`;});
setupDragDivider($('#contract-divider'),ratio=>{const clamped=Math.min(.75,Math.max(.45,ratio));document.documentElement.style.setProperty('--chat-width',`${Math.round((1-clamped)*100)}%`);scheduleContractOutlineUpdate(true);clearTimeout(state._panelSaveTimer);state._panelSaveTimer=setTimeout(async()=>{try{await api('/api/users/me/preferences',{method:'PATCH',body:JSON.stringify({contract_chat_panel_width:1-clamped})});}catch(_){}},500);});

window.addEventListener('DOMContentLoaded',async()=>{try{await refreshBootstrap({keepConversation:false});}catch(error){toast(`MathLab 初始化失败：${error.message}`,true);}});
