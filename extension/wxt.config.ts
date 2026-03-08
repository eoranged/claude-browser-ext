import { defineConfig } from 'wxt';

export default defineConfig({
  manifest: ({ manifestVersion }) => {
    const base = {
      name: 'Claude Browser Bridge',
      version: '0.1.0',
      description: 'Expose browser tab to CLI tools via local server',
      permissions: [
        'storage',
        'scripting',
        'tabs',
        'alarms',
        'webRequest',
      ],
      host_permissions: [
        '<all_urls>',
      ],
      action: {
        default_title: 'Claude Browser Bridge',
        default_icon: {
          '16': 'icon/16.png',
          '32': 'icon/32.png',
          '48': 'icon/48.png',
          '128': 'icon/128.png',
        },
      },
      icons: {
        '16': 'icon/16.png',
        '32': 'icon/32.png',
        '48': 'icon/48.png',
        '128': 'icon/128.png',
      },
    };

    if (manifestVersion === 2) {
      return {
        ...base,
        content_security_policy:
          "script-src 'self'; object-src 'self'; connect-src ws://localhost:* ws://127.0.0.1:* http://localhost:* http://127.0.0.1:*;",
        browser_specific_settings: {
          gecko: {
            id: 'claude-browser-bridge@anthropic.com',
          },
        },
      };
    }

    return base;
  },
  outDir: 'dist',
});
