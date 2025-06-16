from aider.repomap import RepoMap, find_src_files


class SimpleIO:
    def read_text(self, fname: str) -> str:
        with open(fname, "r", encoding="utf-8") as f:
            return f.read()
    def tool_output(self, msg: str):
        print(f"[INFO] {msg}")
    def tool_warning(self, msg: str):
        print(f"[WARN] {msg}")
    def tool_error(self, msg: str):
        print(f"[ERROR] {msg}")

class MockModel:
    def token_count(self, text: str) -> int:
        # 简单模拟：按空格分词后的词数作为 token 数
        return len(text.split())

# Step 1: 构造 IO
io = SimpleIO()

# Step 2: 实例化 RepoMap
rm = RepoMap(
    root="C:\\Users\\phx10\\code\\cg-manager",            # 项目根目录
    io=io,               # 你构造的 IO 实例
    main_model=MockModel(),  # 必须实现 token_count(text) 方法的类（可以 mock）
    verbose=True,
)

# Step 3: 准备文件列表
chat_files = ["C:\\Users\\phx10\\code\\cg-manager\\src\\main\\java\\com\\jialtang\\cg\\manager\\chat\\controller\\ChatController.java"]
other_files = find_src_files("C:\\Users\\phx10\\code\\cg-manager\\src")

print(other_files)

# Step 4: 调用获取 repo map
repo_map = rm.get_repo_map(
    chat_files=chat_files,
    other_files=other_files,
)

print(repo_map)

