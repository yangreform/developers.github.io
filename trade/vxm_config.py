import os
import json

DEFAULT_VXM_CONFIG = {
    "target_init_pos": -2,
    "tp_pnl": 200.0,
    "loss_pnl": -100.0
}

def get_env_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(base_dir, ".env")
    if os.path.exists(env_file):
        return env_file
    op_file = os.path.join(base_dir, "op.env")
    if os.path.exists(op_file):
        return op_file
    return env_file

def load_vxm_config():
    config = {
        "target_init_pos": DEFAULT_VXM_CONFIG["target_init_pos"],
        "tp_pnl": DEFAULT_VXM_CONFIG["tp_pnl"],
        "loss_pnl": DEFAULT_VXM_CONFIG["loss_pnl"]
    }
    env_path = get_env_path()
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("VXM_CONFIG_JSON="):
                        raw_val = line.split("=", 1)[1].strip().strip("'").strip('"')
                        loaded = json.loads(raw_val)
                        if isinstance(loaded, dict):
                            if "target_init_pos" in loaded:
                                config["target_init_pos"] = int(loaded["target_init_pos"])
                            if "tp_pnl" in loaded:
                                config["tp_pnl"] = float(loaded["tp_pnl"])
                            if "loss_pnl" in loaded:
                                config["loss_pnl"] = float(loaded["loss_pnl"])
                            elif "step_losses" in loaded and isinstance(loaded["step_losses"], dict):
                                # 相容舊格式
                                config["loss_pnl"] = float(loaded["step_losses"].get("-2", -100.0))
                        break
        except Exception as e:
            print(f"⚠️ 讀取 .env 的 VXM_CONFIG_JSON 失敗: {e}")
    return config

def save_vxm_config(new_config: dict):
    env_path = get_env_path()
    try:
        clean_cfg = {
            "target_init_pos": int(new_config.get("target_init_pos", -2)),
            "tp_pnl": float(new_config.get("tp_pnl", 200.0)),
            "loss_pnl": float(new_config.get("loss_pnl", -100.0))
        }
        val_str = json.dumps(clean_cfg, ensure_ascii=False)
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.strip().startswith("VXM_CONFIG_JSON="):
                new_lines.append(f"VXM_CONFIG_JSON='{val_str}'\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            new_lines.append(f"\n# =========================\n# VXM 策略參數設定\n# =========================\nVXM_CONFIG_JSON='{val_str}'\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True, clean_cfg
    except Exception as e:
        print(f"⚠️ 寫入 .env 失敗: {e}")
        return False, str(e)
