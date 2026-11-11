"""Laboratory result delivery service.

负责把已确认归属的检测结果以“任务内递增修订”的方式可靠投递到 LIMS，
保证断线重试不重复入库、旧回执不覆盖新修订、进程退出后可沿用同一
投递 ID 与快照继续投递。
"""
