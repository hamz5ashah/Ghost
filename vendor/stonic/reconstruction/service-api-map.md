# Service layer API map

Every module recovered as real source, with its exports. This plus
`main-jsc-symbols.md` is what main.js has to wire back together.

## main.js  (2 lines)

exports: _none detected_

local deps: `./main.jsc`

## preload.js  (290 lines)

exports: _none detected_

## services/account-profile.js  (83 lines)

exports: _none detected_

## services/awareness-focus-watch.js  (172 lines)

exports: _none detected_

## services/awareness-manager.js  (577 lines)

exports: _none detected_

local deps: `./awareness-focus-watch`, `./awareness-timeline`, `./platform-utils`

## services/awareness-timeline.js  (309 lines)

exports: `DAY_CUT_HOURS`, `DayLedger`, `KEEP_MS`, `MIN_SEGMENT_MS`, `TITLE_SETTLE_MS`, `Timeline`, `dayKey`, `humanMinutes`, `sanitizeFocus`

## services/character-report.js  (149 lines)

exports: `createCharacterReporter`, `isCharacterId`

## services/chat-store.js  (211 lines)

exports: `createChatStore`, `looksLikeJsonArray`

## services/computer-manager.js  (1031 lines)

exports: `ComputerManager`

local deps: `./computer-runtime`

## services/computer-overlay.js  (270 lines)

exports: `ComputerOverlay`

local deps: `./computer-runtime`

## services/computer-runtime.js  (191 lines)

exports: `describeRuntime`, `findComputerRuntimeRoot`, `killProcessTree`, `resolvePython`, `resolveRunnerScript`, `resolveTesseract`, `restoreSystemCursors`

local deps: `./python-runtime`

## services/desktop-manager.js  (525 lines)

exports: _none detected_

local deps: `./platform-utils`

## services/desktop-presence-native.js  (617 lines)

exports: `PRESENCE_HELPER_PS1`, `PresenceNative`

## services/desktop-presence.js  (823 lines)

exports: `DesktopPresence`

local deps: `./desktop-presence-native`

## services/facelock-core.js  (254 lines)

exports: `CLEAN_ATTEMPTS`, `EMBEDDING_DIM`, `LOCKOUT_STEPS_MS`, `MATCH_THRESHOLD`, `MAX_FREE_ATTEMPTS`, `MAX_PROBES`, `MAX_TEMPLATES`, `MIN_PROBES`, `MIN_TEMPLATES`, `PIN_LENGTH`, `PROBE_COHERENCE_MIN`, `TEMPLATE_COHERENCE_MIN`, `TICKET_TTL_MS`, `attemptsLeft`, `cosine`, `hashPin`, `isValidPin`, `lockoutRemaining`, `matchProbes`, `newTicket`, `normalizeEmbedding`, `prepareTemplates`, `registerFailure`, `sanitizeState`, `verifyPin`

## services/facelock-manager.js  (259 lines)

exports: `FaceLockManager`

local deps: `./facelock-core`, `./key-store`

## services/found-us-answer.js  (48 lines)

exports: `cleanFoundAnswer`

## services/frame-guard.js  (61 lines)

exports: `isHubPageFrame`, `shouldBlockFrameNavigation`

## services/key-store.js  (99 lines)

exports: `encryptionAvailable`, `readKey`, `readObject`, `writeKeys`, `writeObject`

## services/login-result-page.js  (254 lines)

exports: `loginResultPage`

## services/machine-id.js  (132 lines)

exports: `getMachineId`, `hardwareId`, `pickDisk`

## services/macos-permissions.js  (66 lines)

exports: _none detected_

local deps: `./platform-utils`

## services/macos-updater.js  (356 lines)

exports: `isNewerVersion`, `macDownloadUpdate`, `parseChannelYml`, `setMacAutoDownload`, `setupMacUpdater`

## services/media-browser.js  (280 lines)

exports: `createMediaBrowser`, `runMediaControl`

local deps: `./media-finder`

## services/media-finder.js  (716 lines)

exports: `EMBED_ORIGINS`, `MAX_ITEMS`, `PLATFORM_NAMES`, `collectVideos`, `createMediaFinder`, `decodeHtml`, `durationSeconds`, `isAllowedExternal`, `orderLatest`, `orderRelated`, `parseInitialData`, `parseLink`, `rankForPlay`, `socialTitle`

## services/onboarding-answers.js  (42 lines)

exports: _none detected_

## services/platform-utils.js  (272 lines)

exports: `execPromise`, `findBundledChromiumExecutable`, `getChromiumCandidateSubpaths`, `getDefaultUserAgent`, `isLinux`, `isMac`, `isWindows`, `macGetActiveWindow`, `macListWindows`, `macOpenApplication`

local deps: `./platform-utils`

## services/python-runtime.js  (67 lines)

exports: `findPythonRuntimeRoot`, `pythonHomeDir`

## services/shell-runner.js  (411 lines)

exports: `COMMAND_ENV_VAR`, `POWERSHELL_WRAPPER`, `classifyWindowsCommand`, `cleanPowerShellStderr`, `createWindowsShellRun`, `looksLikePowerShell`, `rewriteCmdEnvVars`, `tidyOutput`, `toExecResult`

## services/staged-updater.js  (1783 lines)

exports: `__test`, `setupStagedUpdater`

## services/static-server.js  (180 lines)

exports: _none detected_

## services/telemetry.js  (366 lines)

exports: `endSession`, `flush`, `init`, `recordOpen`

## services/visual-page-capture.js  (193 lines)

exports: `captureVisualPage`

## services/voice-beta-manager.js  (1518 lines)

exports: `AIMS`, `AVAILABLE_PROVIDERS`, `DEFAULT_MODELS`, `MODES`, `PROVIDERS`, `THINKING`, `VOICES`, `VoiceBetaManager`, `engineLaunch`, `legacyBytecodeDir`, `startTiming`

local deps: `./computer-runtime`, `./key-store`, `./voice-runtime`, `./web-search`

## services/voice-runtime.js  (134 lines)

exports: `describeRuntime`, `findVoiceRuntimeRoot`, `readShippedBytecode`, `resolvePython`, `resolveServerScript`

local deps: `./python-runtime`

## services/weather.js  (179 lines)

exports: `USER_AGENT`, `cleanCoords`, `createWeather`, `shapeForecast`

## services/web-search.js  (355 lines)

exports: `ANTHROPIC_SEARCH_MODEL`, `GEMINI_SEARCH_MODELS`, `MAX_QUERY_CHARS`, `OPENROUTER_SEARCH_MODEL`, `_forgetAll`, `cleanQuery`, `failureText`, `forgetAll`, `formatResult`, `searchAnthropic`, `searchCloud`, `searchGemini`, `searchOpenRouter`, `searchWithOwnKeys`

## services/world-feed.js  (998 lines)

exports: `ATTRIBUTION`, `ATTRIBUTION_URL`, `DEFAULTS`, `OUTLETS`, `QUERIES`, `QUERY_MAX_CHARS`, `USER_AGENT`, `_internals`, `buildQuery`, `buildQueryUrl`, `cleanText`, `cleanTitle`, `createWorldFeed`, `imageInfo`, `isSafeImageUrl`, `normalizeArticle`, `parseGdeltBody`, `parseSeenDate`, `safeHttpsUrl`, `selectItems`, `titleKey`, `urlKey`

## services/skills/library.js  (560 lines)

exports: `MAX_DESCRIPTION_CHARS`, `MAX_NAME_CHARS`, `MAX_WHEN_CHARS`, `PLUGIN_NAME`, `SKILL_FILE`, `SkillsLibrary`, `isValidName`, `legacyFields`, `renderSkill`, `slugify`, `splitSkill`

## services/file-memory/migrate.js  (502 lines)

exports: `BACKUP_DIR`, `MARKER_FILE`, `MIGRATION_VERSION`, `ORIGIN`, `makeBackup`, `parseEntries`, `planMigration`, `readExtras`, `readLegacyDb`, `runMigration`, `slugFrom`

local deps: `./store`

## services/file-memory/official.js  (460 lines)

exports: `CLI_VERSION`, `INDEX_FILE`, `INDEX_MAX_CHARS`, `INDEX_MAX_LINES`, `INSTRUCTIONS_PREAMBLE`, `SYSTEM_REMINDER_NOTE`, `VIRTUAL_ROOT`, `ageNote`, `backfillIndex`, `backfillLine`, `clip`, `dirForPrompt`, `formatListSize`, `formatSize`, `grepResult`, `indexBlock`, `indexHeading`, `indexWriteNotice`, `loadIndex`, `memorySection`, `memoryTool`, `numberedLine`, `sanitizeText`, `stampNote`, `stripIndexExtras`

## services/file-memory/rules.js  (49 lines)

exports: `buildAgentRules`, `buildClassicCard`

local deps: `./official`

## services/file-memory/service.js  (301 lines)

exports: `FileMemoryService`

local deps: `./migrate`, `./official`, `./rules`, `./store`

## services/file-memory/store.js  (835 lines)

exports: `COMMANDS`, `FileMemory`, `INDEX_FILE`, `LIMITS`, `MemoryError`, `VIRTUAL_ROOT`, `globToRegExp`, `parseNote`, `writeAtomic`

local deps: `./official`

## services/memory/config.js  (278 lines)

exports: `AGENT`, `AUTOMATED_PREFIXES`, `BACKUP`, `BUDGETS`, `CHANNELS`, `DAY`, `DIGEST`, `FILES`, `HOUR`, `INTENTIONS`, `JOBS`, `LIMITS`, `MEMORY_STATUSES`, `MEMORY_TYPES`, `MIN`, `MINDS`, `ORIGINS`, `SEARCH`, `SINGLE_VALUED_TYPES`, `SPEAKERS`, `TURN_FLAGS`

## services/memory/consolidate.js  (76 lines)

exports: _none detected_

local deps: `./config`, `./db`

## services/memory/context.js  (279 lines)

exports: `MemoryContext`, `sourceLabel`

local deps: `./config`, `./store`, `./timefmt`

## services/memory/db.js  (291 lines)

exports: `backupDir`, `backupDue`, `backupNow`, `closeMemoryDb`, `dbPath`, `getDriverError`, `listBackups`, `loadDriver`, `memoryDir`, `mirrorBackup`, `openMemoryDb`, `pruneBackups`, `quickCheck`, `verifyBackup`

local deps: `./config`, `./schema`

## services/memory/digest.js  (433 lines)

exports: `MemoryDigest`

local deps: `./config`, `./normalize`, `./store`, `./timefmt`

## services/memory/jobs.js  (127 lines)

exports: _none detected_

local deps: `./config`

## services/memory/lexicon.js  (205 lines)

exports: `GROUP_COUNT`, `synonymsOf`

local deps: `./normalize`

## services/memory/llm-relay.js  (74 lines)

exports: _none detected_

## services/memory/normalize.js  (669 lines)

exports: `STOPWORDS`, `contentHash`, `detectScript`, `foldText`, `normalizeForSearch`, `queryTokens`, `skeleton`

## services/memory/recall-agent.js  (500 lines)

exports: `HEADS`, `INSTRUCTIONS`, `RecallAgent`, `parseReply`

local deps: `./config`, `./store`, `./timefmt`

## services/memory/schema.js  (354 lines)

exports: `MIGRATIONS`, `SCHEMA_VERSION`

## services/memory/search.js  (526 lines)

exports: `MemorySearch`

local deps: `./config`, `./lexicon`, `./normalize`, `./store`, `./timefmt`

## services/memory/secrets.js  (846 lines)

exports: `hasSecret`, `redactSecrets`

## services/memory/service.js  (813 lines)

exports: `MemoryService`, `scopesFor`

local deps: `./config`, `./consolidate`, `./context`, `./db`, `./digest`, `./jobs`, `./normalize`, `./recall-agent`, `./search`, `./store`, `./timefmt`

## services/memory/store.js  (809 lines)

exports: `MemoryStore`, `parseJson`, `scopesFor`, `sessionKey`

local deps: `./config`, `./normalize`, `./secrets`

## services/memory/timefmt.js  (153 lines)

exports: `WEEKDAY_NAMES`, `absoluteDate`, `clockText`, `dueText`, `parseWhen`, `relativeTime`, `startOfDay`, `whenText`

## pwa/app.js  (413 lines)

exports: _none detected_

## pwa/sw.js  (43 lines)

exports: _none detected_

## main/shared/paths.js  (29 lines)

exports: _none detected_

## main/ipc/awareness-handlers.js  (23 lines)

exports: _none detected_

## main/ipc/computer-handlers.js  (25 lines)

exports: _none detected_

## main/ipc/facelock-handlers.js  (35 lines)

exports: _none detected_

## main/ipc/file-memory-handlers.js  (94 lines)

exports: _none detected_

## main/ipc/index.js  (96 lines)

exports: `getAwarenessManager`, `getComputerManager`, `getFaceLockManager`, `getFileMemoryService`, `getMainWindow`, `getMemoryService`, `getVoiceBetaManager`, `registerDomainHandlers`, `setAwarenessManager`, `setComputerManager`, `setFaceLockManager`, `setFileMemoryService`, `setMainWindow`, `setMemoryService`, `setVoiceBetaManager`

local deps: `./awareness-handlers`, `./computer-handlers`, `./facelock-handlers`, `./file-memory-handlers`, `./memory-handlers`, `./onboarding-handlers`, `./skills-handlers`, `./system-handlers`, `./tutorial-handlers`, `./voice-beta-handlers`, `./weather-handlers`, `./whatsapp-handlers`, `./world-feed-handlers`

## main/ipc/memory-handlers.js  (111 lines)

exports: _none detected_

local deps: `../../services/memory/llm-relay`

## main/ipc/onboarding-handlers.js  (75 lines)

exports: `cleanCpu`, `registerOnboardingHandlers`

## main/ipc/skills-handlers.js  (128 lines)

exports: `getCustomSkillsDir`, `registerSkillsHandlers`

local deps: `../../services/skills/library`

## main/ipc/system-handlers.js  (684 lines)

exports: _none detected_

local deps: `../../services/key-store`, `../../services/shell-runner`, `../../services/visual-page-capture`, `../shared/paths`

## main/ipc/tutorial-handlers.js  (59 lines)

exports: `registerTutorialHandlers`, `tutorialVideoPath`

## main/ipc/voice-beta-handlers.js  (45 lines)

exports: _none detected_

## main/ipc/weather-handlers.js  (40 lines)

exports: _none detected_

local deps: `../../services/weather`

## main/ipc/whatsapp-handlers.js  (162 lines)

exports: _none detected_

## main/ipc/world-feed-handlers.js  (127 lines)

exports: `registerWorldFeedHandlers`

local deps: `../../services/world-feed`

## main/bootstrap/stonic-bootstrap.js  (58 lines)

exports: `ensureStonicDirectories`, `getStonicBootstrapDir`, `getStonicMemoryDir`
