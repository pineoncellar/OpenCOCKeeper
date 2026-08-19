/*- encoding: utf-8 -*/
/*
@File     :   notifier.js
@Desc     :   叙事完成提醒 — 纯网页前端：标签页标题闪烁 + Web Audio 提示音
@Note     :   仅当页面处于后台(document.hidden)时触发,避免打扰正在观看的玩家;
             纯前端、所有浏览器/网络环境(含局域网 http)均可用,无需浏览器权限;
             设置存 localStorage
*/

const TurnNotifier = {
    STORAGE_KEY: 'coc.notify.enabled',

    init() {
        this.enabled = localStorage.getItem(this.STORAGE_KEY) !== 'false';
        this._flashTimer = null;
        this._origTitle = document.title;
    },

    // 叙事完成时调用:页面在后台才提醒
    notify() {
        if (!this.enabled) return;
        if (!document.hidden) return;      // 页面可见时不打扰
        this._flashTitle();
        this._playBeep();
    },

    // ----------------------------------------------------------------
    // 标题闪烁(所有网络环境可用;切到其他标签页时任务栏也会闪)
    // ----------------------------------------------------------------

    _flashTitle() {
        this._origTitle = document.title;
        let flip = false;
        const titles = ['【叙事完成】OpenCOCKeeper', this._origTitle || 'OpenCOCKeeper'];
        this._stopFlash();
        this._flashTimer = setInterval(() => {
            flip = !flip;
            document.title = flip ? titles[0] : titles[1];
        }, 900);
        // 页面回到前台时立即停止并恢复标题
        const onVis = () => {
            if (!document.hidden) {
                this._stopFlash();
                document.removeEventListener('visibilitychange', onVis);
            }
        };
        document.addEventListener('visibilitychange', onVis);
    },

    _stopFlash() {
        if (this._flashTimer) {
            clearInterval(this._flashTimer);
            this._flashTimer = null;
        }
        document.title = this._origTitle || document.title;
    },

    // ----------------------------------------------------------------
    // 提示音(Web Audio 合成,无需音频文件;用户与页面交互过后可正常播放)
    // ----------------------------------------------------------------

    _playBeep() {
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            const ctx = new Ctx();
            if (ctx.state === 'suspended') ctx.resume();
            const now = ctx.currentTime;
            // 两短一长的"叙事到达"提示音
            const seq = [[0, 880, 0.12], [0.16, 880, 0.12], [0.32, 1174.66, 0.22]];
            seq.forEach(([t, freq, dur]) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0.0001, now + t);
                gain.gain.exponentialRampToValueAtTime(0.2, now + t + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, now + t + dur);
                osc.connect(gain).connect(ctx.destination);
                osc.start(now + t);
                osc.stop(now + t + dur + 0.02);
            });
        } catch (e) { /* 忽略音频异常 */ }
    },
};

// 挂到 window 供调试/外部脚本访问（const 顶层声明不会自动成为 window 属性）
window.TurnNotifier = TurnNotifier;
