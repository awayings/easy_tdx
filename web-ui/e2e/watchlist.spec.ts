// 自选页 E2E：加入自选（行情校验 + 名称补全走 mock）→ 表格出现 → 删除 → 消失。
// 另覆盖 issue #7 的「近3日 / 近1周 / 近2周」三列（交易日偏移口径，锚点走后端
// /watchlist/returns，涨跌幅由前端用实时价现算）。
//
// 每轮 E2E 用独立的临时 EASY_TDX_CONFIG_DIR，自选从空开始，断言可写死。

import { expect, test } from '@playwright/test'

test('自选页增删自选', async ({ page }) => {
  await page.goto('/watchlist')

  // 初始为空（临时配置目录）
  await expect(page.locator('.empty-row')).toBeVisible()
  // 空行 colspan 与表头列数一致（新增 3 列后 = 15）
  await expect(page.locator('.empty-row td')).toHaveAttribute('colspan', '15')

  // 加入 600519（市场自动识别 SH；名称走 mock /mac/symbol-info → 贵州茅台）
  await page.fill('.code-input', '600519')
  await page.getByRole('button', { name: '加入自选' }).click()
  await expect(page.locator('.data-row')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.locator('.data-row .cell-name')).toHaveText('贵州茅台')
  await expect(page.locator('.data-row .cell-code')).toHaveText('SH600519')

  // 删除后表格回到空态
  await page.locator('.data-row .del').first().click()
  await expect(page.locator('.data-row')).toHaveCount(0)
  await expect(page.locator('.empty-row')).toBeVisible()
})

test('自选页近3日/近1周/近2周涨跌幅三列', async ({ page }) => {
  await page.goto('/watchlist')

  await page.fill('.code-input', '600519')
  await page.getByRole('button', { name: '加入自选' }).click()
  await expect(page.locator('.data-row')).toHaveCount(1, { timeout: 30_000 })

  // 表头：现价/涨跌幅之后依次是 近3日、近1周、近2周（共 15 列 = 12 + 3）
  const headers = page.locator('.qtable thead th')
  await expect(headers).toHaveCount(15)
  await expect(headers.nth(2)).toHaveText('涨跌幅')
  await expect(headers.nth(3)).toHaveText('近3日')
  await expect(headers.nth(4)).toHaveText('近1周')
  await expect(headers.nth(5)).toHaveText('近2周')

  // 数据格：与表头列数一致，三列都是带符号百分比（合成行情锚点 → 一定会算出数）
  const cells = page.locator('.data-row td')
  await expect(cells).toHaveCount(15)
  for (const i of [3, 4, 5]) {
    await expect(cells.nth(i)).toHaveText(/^[+-]?\d+\.\d+%$/)
  }

  await page.locator('.data-row .del').first().click()
  await expect(page.locator('.data-row')).toHaveCount(0)
})
