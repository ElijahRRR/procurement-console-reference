// Service Worker for Chrome Extension
// 处理cookie获取请求

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'getCookies') {
    const url = request.url;
    
    chrome.cookies.getAll({ url: url }, (cookies) => {
      if (chrome.runtime.lastError) {
        sendResponse({ 
          success: false, 
          error: chrome.runtime.lastError.message 
        });
        return;
      }
      
      // 格式化cookie数据，添加id字段
      const formattedCookies = cookies.map((cookie, index) => {
        return {
          domain: cookie.domain,
          expirationDate: cookie.expirationDate,
          hostOnly: cookie.hostOnly,
          httpOnly: cookie.httpOnly,
          name: cookie.name,
          path: cookie.path,
          sameSite: cookie.sameSite || 'no_restriction',
          secure: cookie.secure,
          session: cookie.session,
          storeId: cookie.storeId || '1',
          value: cookie.value,
          id: index + 1
        };
      });
      
      sendResponse({ 
        success: true, 
        cookies: formattedCookies 
      });
    });
    
    // 返回true表示异步响应
    return true;
  }
});

