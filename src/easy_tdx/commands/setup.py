"""新式握手命令（2026-09 起主站强制要求，勿回退旧三条命令）。

历史（2026-09-10 与 eltdx 逐字节比对 + 对照实测定案）：

2026-09 起行情主站拒绝"旧版客户端握手"建立的连接：旧握手 = pytdx 三条
固定 msg_id 的 0x000d 命令（0x1893/0x1894/0x1899，payload 01/02/签名串）。
握手本身有响应，但连接上所有 K 线请求一律返回 2 字节空包（0x0320，声称
800 条）、880xxx 统计指数快照返回空——服务器不报错，只是不给数据
（部分主站 setup2 响应明示"客户端与行情主站不匹配"）。web /market/stat
500、/bars/index 空列表均源于此。

对照实验（三组）：
  1. 旧三条命令 + 随机 msg_id → 仍被拒（拒绝标记是三条命令序列本身）；
  2. 新式单条握手 + 固定 msg_id 的业务请求 → 全部正常；
  3. 新式握手连接上连续 8 个固定 msg_id 业务请求 → 全部正常。

新式握手 = 单条 0x000d 命令、payload 0x01、msg_id 随机（每连接新生成）。
业务请求格式（28 字节 K 线 / 0x053e 快照等）无需任何改动。
"""

import random
import struct


def build_handshake_command() -> bytes:
    """生成一条新式握手命令（msg_id 每次调用随机生成）。

    旧三条 setup 常量（SETUP_CMD1/2/3）已删除；发送握手一律走本函数。
    """
    msg_id = random.randint(1, 0xFFFFFFFE)
    return struct.pack("<HIHHH", 0x010C, msg_id, 0x0003, 0x0003, 0x000D) + b"\x01"
