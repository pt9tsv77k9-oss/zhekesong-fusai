// 不修改官方 Skill：从标准输入读取正文，再调用其导出的统一入口。
// 长篇中文笔记不会占用 Windows 命令行参数空间，也不写入临时文件。
'use strict';
// .cjs 表示 Node.js 的 CommonJS 模块；require 类似 Python 的 import。
// strict 开启严格模式，帮助尽早发现意外的变量使用。

const fs = require('node:fs');
const path = require('node:path');

async function main() {
  // async 让函数可以使用 await 等待异步接口；本文件只充当 Python 和官方脚本的桥梁。
  try {
    // argv 前两项是 node 程序路径和当前脚本路径；实际参数从第三项开始。
    const [, , scriptPath, endpoint] = process.argv;
    if (!scriptPath || !endpoint) throw new Error('缺少 ima 脚本或接口参数。');
    const body = JSON.parse(fs.readFileSync(0, 'utf8'));
    // 文件描述符 0 就是 stdin，Python 通过 input=... 送来完整 JSON。
    // 这里不把正文或凭证放进命令行，也不会为长笔记创建临时文件。
    const { imaApi } = require(path.resolve(scriptPath));
    // 复用官方导出的 imaApi；endpoint 已由 Python 白名单限制。
    const response = await imaApi(endpoint, body);
    // stdout 只能输出官方返回的 JSON 字符串，否则 Python 的 json.loads 会失败。
    process.stdout.write(response);
  } catch (error) {
    // 失败信息写 stderr，退出码设为 1；Python 会据此区分脚本失败/业务失败。
    // 不直接输出整个 error 对象，避免混入原始请求等不适合页面展示的信息。
    process.stderr.write(JSON.stringify({
      code: typeof error.code === 'number' ? error.code : -100,
      msg: error.msg || 'ima 调用未完成，请检查官方脚本、请求内容及网络。',
    }));
    process.exitCode = 1;
  }
}

main();
