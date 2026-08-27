/*- encoding: utf-8 -*/
/*
@File     :   drawer.js
@Desc     :   移动端覆盖式抽屉交互 — 浮动按钮 + 遮罩开合
@Note     :   Trace(世界/轮次)、Worlds(世界列表)、Game(角色面板) 共用；
             通过给 layout 挂 openClass 触发 CSS 滑入/滑出；
             桌面端(>640px)按钮与遮罩由 CSS 隐藏，绑定事件无副作用
*/

/**
 * 初始化一个移动端覆盖式抽屉。
 * @param {Object} opts
 * @param {HTMLElement} opts.layout    抽屉所属布局容器（挂 openClass 标记开合）
 * @param {string}      opts.openClass 打开时加在 layout 上的 class（如 'sidebar-worlds'）
 * @param {HTMLElement} opts.button    浮动按钮（点击开合）
 * @param {HTMLElement} opts.mask      全屏遮罩（点击关闭）
 * @param {HTMLElement} [opts.drawer]  抽屉元素（用于 onSelect 事件委托）
 * @param {Function}    [opts.onOpen]  打开前的回调（用于互斥关闭其它抽屉）
 * @param {Function}    [opts.onSelect] (event)=>boolean；抽屉内选中条目返回 true 则自动收起
 * @returns {{open: Function, close: Function, toggle: Function, isOpen: Function}|null}
 */
function initMobileDrawer(opts) {
    const { layout, openClass, button, mask } = opts;
    if (!layout || !mask || !button) return null;

    const isOpen = () => layout.classList.contains(openClass);
    const refresh = () => mask.classList.toggle('show', isOpen());
    const close = () => {
        layout.classList.remove(openClass);
        refresh();
    };
    const open = () => {
        if (opts.onOpen) opts.onOpen();
        layout.classList.add(openClass);
        refresh();
    };
    const toggle = () => (isOpen() ? close() : open());

    button.addEventListener('click', toggle);
    mask.addEventListener('click', close);
    if (opts.drawer && opts.onSelect) {
        opts.drawer.addEventListener('click', (e) => {
            if (opts.onSelect(e)) close();
        });
    }
    return { open, close, toggle, isOpen };
}
