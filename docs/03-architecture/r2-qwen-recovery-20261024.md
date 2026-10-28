# Qwen共享工作进程故障的健康会话恢复（S2窄修）

依据原CAP11“其他会话不被误杀”，以及两项红测（内部记录，未公开）。本项修复新provider未达到的原合同，不新增业务接口、第三方资源、模型或数据外发，也不改变R2/分类/词库。候选codec已用3段授权原音完成6路连续/逐块恢复对照、3组逐字相同，见完整边界（内部记录，未公开）。该证据只允许进入控制面实现验证，不直接作为CAP11验收。

## 原因与目标

原一个worker内保存多个原生状态，单路强停丢失全代状态；父层直接invalidate所有流，甚至尚未派发的流也失败。空闲owner的drop排在健康decode之后超过0.5秒，也走此全代失败。强停仍是清理运行中模型的必要手段，不能去掉或放宽D+1秒PCM/D+2秒进程退出确认。目标是保留健康流最后确认的状态与未确认pending，在同一用户会话和原绝对预算内恢复。

## 有界快照与身份

每次成功feed的worker响应包含`svi-qwen-native-checkpoint-v1`：owner三元组、已交付样本、nonzero、固定v0.4.4字段、float32活窗口和原生pending。父层先验证样本/owner/词表与响应正文、committed前缀一致，才替换上次快照和推进decoded游标；不保存失败或未确认的状态。worker内checkpoint输入只允许在该owner原生state缺失时恢复，已有state不得被替换；start_sample必须等于快照samples。新流start=0无需快照。

codec字段/长度上限见原实验源。声学两个bytearray最多2047996字节/owner，metadata最多1MiB；当前最多20业务owner有界。它属于会话工作副本，不新增持久音频或扩大600秒重试缓存资格。clear同步清数组、token列表、metadata，取消/TTL/error/finish/close路径均必须清理。快照不携带模型/tokenizer对象或路径，不复制encoder/KV加速缓存；恢复后仅重算加速缓存，保留声学、rollback和已提交文本。

## 进程代与恢复

共享故障先将ready置false，推进物理generation，清所有在途传输工作副本；仅故障/取消者失效并同步释放其快照。健康流有完整快照或decoded=0时保留pending、options、revision、text及样本计数，并绑定新代；缺失/非法快照明确失败，禁止空状态接尾。每个流另记最后确认的native_generation，重启后首次派发才附带其快照。

旧进程terminate/kill并确认退出前，原scheduler执行许可不归还、不启动新代。并发清理加入同一个stop task。确认之后启动唯一prepare task；在固定模型实际预热成功前不ready，不允许另起20份模型。健康流在原有操作deadline内等待该prepare，等待者取消不取消共享prepare；准备失败明确不可用。

若健康请求被另一owner导致的物理generation更替中断，只用最后确认快照和仍在父pending的同一未确认PCM重派发。物理调用及失败结果分别记账，不重复发布partial、推进游标或创建业务会话；固定使用最初的eligibility/outcome deadline，不重置3秒feed、10秒finish或30秒停止预算。期限不足则按原规则失败，而非续期。被取消者、已失效者、关闭provider及迟到旧代输出绝不进入恢复循环。

空闲drop仍优先走目标owner删除；若因另一请求占用超时而需强停，健康在途请求按上述方式恢复。只有失败/取消者的快照同步清空，健康快照保留至各自合法结束；新代第一次成功feed替换旧快照时释放旧数组。已成功finish的快照立即清空，不为历史重整保留音频。

## 通过门

先编译；原20owner两负控不改判据回绿；新增快照身份/越界/已确认游标/清理故障控制；实际OS旧PID退出后才能新PID准备、健康原会话继续；原Qwen/调度/预算/账本全量回归；必要自然TTL和真实模型恢复。未通过前CAP11继续fail，不提高60/137。模型正常识别与官方R2提示不变；对真实恢复产生的文字变化如实留反例，不能以缓存重建理由忽略已提交前缀/样本丢失。
