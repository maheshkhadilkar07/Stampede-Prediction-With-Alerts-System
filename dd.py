import cv2

url = "http://192.168.43.225:8080/video"   # Change to your phone's IP

cap = cv2.VideoCapture(url)

print("Opened:", cap.isOpened())

while True:
    ret, frame = cap.read()

    if not ret:
        print("Frame not received")
        break

    cv2.imshow("IP Camera", frame)

    if cv2.waitKey(1) == 27:
        break

cap.release()
cv2.destroyAllWindows()