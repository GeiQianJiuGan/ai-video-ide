// 渲染 Tauri 壳注入的 window.__AIVS_BOOT_ERROR__。
// 契约与后端错误一致：{ code, title, detail, suggestions }。
;(function () {
  var err = window.__AIVS_BOOT_ERROR__ || {
    code: 'INTERNAL',
    title: '启动失败',
    detail: '桌面壳没有注入失败详情，这本身就是一个 bug，请查看 .runtime/backend.stderr.log。',
    suggestions: [],
  }

  document.getElementById('title').textContent = err.title || '启动失败'
  document.getElementById('code').textContent = err.code || ''
  document.getElementById('detail').textContent = err.detail || '（无）'

  var list = err.suggestions || []
  if (list.length) {
    var ol = document.getElementById('suggestions')
    for (var i = 0; i < list.length; i++) {
      var li = document.createElement('li')
      li.textContent = list[i]
      ol.appendChild(li)
    }
    document.getElementById('suggestions-panel').hidden = false
  }
})()
