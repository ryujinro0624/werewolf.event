from app import send_daily_lottery_summary

if __name__ == "__main__":
    count = send_daily_lottery_summary()
    print(f"抽選応募集計メールを{count}件送信しました。")
